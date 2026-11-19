"""probe names the EasyBuild install guide when eb is not on PATH."""

import tempfile
import unittest

from tests.support import run_cli


class MissingEasybuildTests(unittest.TestCase):
    def test_probe_says_how_to_install_easybuild(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = run_cli("probe", "--path", directory, "--eb", "missing-eb-for-this-test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("missing-eb-for-this-test was not found on PATH", proc.stdout)
        self.assertIn("Install EasyBuild and put eb on PATH", proc.stdout)
        self.assertIn("https://docs.easybuild.io/installation/", proc.stdout)
        self.assertIn("pass --eb with its path", proc.stdout)


if __name__ == "__main__":
    unittest.main()
