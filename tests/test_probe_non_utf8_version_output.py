"""Regression test for non-UTF-8 probe command output."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


def _python_probe(path: str, eb: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return subprocess.run(
        [sys.executable, "-m", "eb_staged_rebuild", "probe", "--json", "--path", path, "--eb", eb],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _zig_probe(path: str, eb: str) -> subprocess.CompletedProcess[str]:
    repo = pathlib.Path(__file__).resolve().parents[1]
    zig_bin = repo / "zig-out" / "bin" / "eb-staged-rebuild"
    if not zig_bin.exists():
        raise unittest.SkipTest(f"zig binary not found at {zig_bin}")
    return subprocess.run(
        [str(zig_bin), "probe", "--json", "--path", path, "--eb", eb],
        capture_output=True,
        text=True,
        check=False,
    )


class ProbeNonUtf8VersionOutputTests(unittest.TestCase):
    def test_non_utf8_eb_version_output_keeps_probe_json(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            eb = os.path.join(root, "eb")
            with open(eb, "wb") as handle:
                handle.write(
                    b"#!/bin/sh\n"
                    b'if [ "$1" = "--version" ]; then printf "\\300\\252\\n"; exit 0; fi\n'
                    b"exit 0\n"
                )
            os.chmod(eb, 0o755)

            py = _python_probe(root, eb)
            zig = _zig_probe(root, eb)

            self.assertEqual(py.returncode, 0, py.stdout + py.stderr)
            self.assertEqual(zig.returncode, 0, zig.stdout + zig.stderr)
            self.assertNotIn("Traceback", py.stderr)
            py_easybuild = json.loads(py.stdout)["easybuild"]
            zig_easybuild = json.loads(zig.stdout)["easybuild"]
            self.assertEqual(py_easybuild["framework_version"], zig_easybuild["framework_version"])
            self.assertEqual(py_easybuild["stage_path_keys"], zig_easybuild["stage_path_keys"])


if __name__ == "__main__":
    unittest.main()
