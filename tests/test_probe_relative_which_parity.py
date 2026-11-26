"""probe --json reports executable paths found through a relative PATH entry as relative.

A relative PATH directory and an empty PATH entry stay relative.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.support import run_cli


def _script(path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, 0o755)


def _paths(proc):
    data = json.loads(proc.stdout)
    return data["bwrap"]["path"], data["userns_sandbox"]["path"]


class ProbeRelativeWhichTests(unittest.TestCase):
    def _probe(self, tmp: str, path_env: str):
        work = os.path.join(tmp, "work")
        os.makedirs(work, exist_ok=True)
        env = {"PATH": path_env, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
        args = ("probe", "--json", "--no-create", "--path", work, "--eb", "/bin/true")
        done = run_cli(*args, env=env, cwd=tmp)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stderr, "")
        return _paths(done)

    def test_a_relative_path_directory_stays_relative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            for name in ("bwrap", "unshare"):
                _script(os.path.join(bindir, name))
            bwrap, unshare = self._probe(tmp, "bin")
            self.assertEqual(bwrap, "bin/bwrap")
            self.assertEqual(unshare, "bin/unshare")

    def test_an_empty_path_entry_stays_a_relative_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("bwrap", "unshare"):
                _script(os.path.join(tmp, name))
            bwrap, unshare = self._probe(tmp, ":")
            self.assertEqual(bwrap, "bwrap")
            self.assertEqual(unshare, "unshare")


if __name__ == "__main__":
    unittest.main()
