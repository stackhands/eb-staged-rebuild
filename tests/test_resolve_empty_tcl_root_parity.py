"""Parity check for resolve output with an empty quoted Tcl root."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


def _python_resolve(path: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return subprocess.run(
        [sys.executable, "-m", "eb_staged_rebuild", "resolve", path],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _zig_resolve(path: str) -> subprocess.CompletedProcess[str]:
    repo = pathlib.Path(__file__).resolve().parents[1]
    zig_bin = repo / "zig-out" / "bin" / "eb-staged-rebuild"
    if not zig_bin.exists():
        raise unittest.SkipTest(f"zig binary not found at {zig_bin}")
    return subprocess.run(
        [str(zig_bin), "resolve", path],
        capture_output=True,
        text=True,
        check=False,
    )


class ResolveEmptyTclRootParityTests(unittest.TestCase):
    def test_resolve_empty_quoted_tcl_root_matches_zig(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            module = os.path.join(root, "empty.tcl")
            with open(module, "w", encoding="utf-8") as handle:
                handle.write('set root ""\n')

            py = _python_resolve(module)
            zig = _zig_resolve(module)

            self.assertEqual(py.returncode, 1, py.stdout + py.stderr)
            self.assertEqual(zig.returncode, 1, zig.stdout + zig.stderr)
            self.assertEqual(py.stderr, "")
            self.assertEqual(zig.stderr, "")
            py_doc = json.loads(py.stdout)
            zig_doc = json.loads(zig.stdout)
            py_doc.pop("path", None)
            zig_doc.pop("path", None)
            self.assertEqual(py_doc, zig_doc)
            self.assertEqual(py_doc["syntax"], "tcl")
            self.assertEqual(py_doc["root"], "")


if __name__ == "__main__":
    unittest.main()
