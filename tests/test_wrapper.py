"""The Python package finds the binary and replaces itself with it."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import eb_staged_rebuild
from tests.support import tool_bin

REPO = Path(__file__).resolve().parents[1]
BUNDLED = Path(eb_staged_rebuild.__file__).resolve().parent / "bin" / "eb-staged-rebuild"


def python(*args, path):
    """``python -m eb_staged_rebuild ARGS`` with PATH set to PATH and this checkout importable."""
    env = {**os.environ, "PATH": path, "PYTHONPATH": str(REPO)}
    return subprocess.run([sys.executable, "-m", "eb_staged_rebuild", *args], capture_output=True, text=True, env=env)


class LocatorTests(unittest.TestCase):
    def path_with_binary(self):
        directory = tempfile.mkdtemp(prefix="wrapper-")
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        os.symlink(tool_bin(), os.path.join(directory, "eb-staged-rebuild"))
        return directory

    def test_a_binary_on_path_is_found_when_none_is_bundled(self):
        if BUNDLED.exists():
            self.skipTest("the package carries a binary")
        directory = self.path_with_binary()
        done = python("--version", path=directory)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith("eb-staged-rebuild "), done.stdout)

    def test_no_binary_is_a_clear_refusal(self):
        if BUNDLED.exists():
            self.skipTest("the package carries a binary")
        empty = tempfile.mkdtemp(prefix="wrapper-empty-")
        self.addCleanup(os.rmdir, empty)
        done = python("--version", path=empty)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stdout, "")
        self.assertIn("eb-staged-rebuild: no eb-staged-rebuild binary", done.stderr)
        self.assertIn("is not on PATH", done.stderr)

    def test_binary_path_raises_when_there_is_none(self):
        if BUNDLED.exists():
            self.skipTest("the package carries a binary")
        saved = os.environ["PATH"]
        os.environ["PATH"] = ""
        try:
            with self.assertRaises(FileNotFoundError):
                eb_staged_rebuild.binary_path()
        finally:
            os.environ["PATH"] = saved


class ExecTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.mkdtemp(prefix="wrapper-")
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        os.symlink(tool_bin(), os.path.join(directory, "eb-staged-rebuild"))
        self.path = directory

    def test_the_module_prints_what_the_binary_prints(self):
        direct = subprocess.run([tool_bin(), "--help"], capture_output=True, text=True)
        wrapped = python("--help", path=self.path)
        self.assertEqual((wrapped.returncode, wrapped.stdout, wrapped.stderr), (direct.returncode, direct.stdout, direct.stderr))

    def test_the_exit_status_and_the_streams_pass_through(self):
        missing = "/definitely/not/a/module.lua"
        direct = subprocess.run([tool_bin(), "resolve", missing], capture_output=True, text=True)
        wrapped = python("resolve", missing, path=self.path)
        self.assertEqual(direct.returncode, 1)
        self.assertEqual((wrapped.returncode, wrapped.stdout, wrapped.stderr), (direct.returncode, direct.stdout, direct.stderr))

    def test_run_json_parses_a_probe_document(self):
        saved = os.environ["PATH"]
        os.environ["PATH"] = self.path + os.pathsep + saved
        try:
            with tempfile.TemporaryDirectory() as tmp:
                document = eb_staged_rebuild.run_json("probe", "--path", tmp, "--eb", "no-such-eb")
        finally:
            os.environ["PATH"] = saved
        self.assertIn("rename_exchange", document)
        self.assertFalse(document["easybuild"]["found"])

    def test_the_version_names_the_package_version(self):
        done = python("--version", path=self.path)
        self.assertIn(f" {eb_staged_rebuild.__version__} ", done.stdout)


if __name__ == "__main__":
    unittest.main()
