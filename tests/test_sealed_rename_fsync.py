"""A failed directory flush after a sealed software rename stops the run.

The staged tree is renamed beside the live tree, and that directory is
flushed afterwards. Python raises. The binary ignored the flush and
published the run. The tracer has to be the parent of the release binary.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from tests.support import classic_install

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FAKE = os.path.join(_REPO, "tests", "fake_eb.py")


def _python314():
    if sys.version_info[:2] == (3, 14):
        return sys.executable
    found = shutil.which("python3.14")
    if not found:
        return None
    completed = subprocess.run(
        [found, "-c", "import sys; raise SystemExit(0 if sys.version_info[:2]==(3,14) else 1)"],
        check=False,
    )
    return found if completed.returncode == 0 else None


def _program():
    """Build tests/fs_fail.c. The release binary is static, so the tracer has to be its parent."""
    src = os.path.join(_REPO, "tests", "fs_fail.c")
    out = os.path.join(tempfile.gettempdir(), "eb-fs-fail-n22")
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(src):
        subprocess.run(["gcc", "-O2", "-o", out, src], check=True)
    return out


class SealedRenameFsyncTest(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("TOOL_BIN"):
            self.skipTest("TOOL_BIN is unset")
        self.python = _python314()
        if self.python is None:
            self.skipTest("python3.14 is not installed")

    def _layout(self):
        root = tempfile.mkdtemp(prefix="sealed-fsync-")
        self.addCleanup(shutil.rmtree, root, True)
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        os.makedirs(software)
        os.makedirs(modules)
        classic_install(software, modules, "demo/1", "old\n", "all/demo/1.lua")
        eb = os.path.join(root, "eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/sh\nexec {self.python} {_FAKE} \"$@\"\n")
        os.chmod(eb, stat.S_IRWXU)
        args = [
            "run",
            "--software",
            software,
            "--modules",
            modules,
            "--state-root",
            state,
            "--backend",
            "installpath",
            "--eb",
            eb,
            "--run-id",
            "run1",
            "--skip-live-check",
            "--lock-timeout",
            "0",
            "--module",
            "demo/1",
            "--",
            "demo-1.eb",
        ]
        env = {
            "PYTHONPATH": _REPO,
            "PYTHONIOENCODING": "utf-8:strict",
            "LANG": "C.UTF-8",
            "FAKE_EB_VERSION": "5.4.2",
            "FAKE_EB_PAYLOAD": "new\n",
            "FAKE_EB_REL": "demo/1",
            "PATH": os.environ.get("PATH", ""),
        }
        return root, env, args

    def _payload(self, root):
        with open(os.path.join(root, "software", "demo", "1", "PAYLOAD"), encoding="utf-8") as handle:
            return handle.read()

    def _journal(self, root):
        with open(os.path.join(root, "state", "runs", "run1", "journal.json"), encoding="utf-8") as handle:
            return json.load(handle).get("state")

    def test_failed_directory_flush_stops_the_sealed_rename(self):
        # The third fsync under software/demo is the directory flush after
        # the staged tree is renamed beside the live tree.
        tool = os.environ["TOOL_BIN"]
        program = _program()
        py_root, py_env, py_args = self._layout()
        zig_root, zig_env, zig_args = self._layout()
        py = subprocess.run(
            [program, "5", "fsync", "3", "software/demo", "--", self.python, "-m", "eb_staged_rebuild.cli", *py_args],
            cwd=py_root,
            env=py_env,
            capture_output=True,
            text=True,
            check=False,
        )
        zig = subprocess.run(
            [program, "5", "fsync", "3", "software/demo", "--", tool, *zig_args],
            cwd=zig_root,
            env=zig_env,
            capture_output=True,
            text=True,
            check=False,
        )
        detail = py.stderr + "\n---\n" + zig.stderr + zig.stdout
        self.assertEqual(py.returncode, 2, py.stdout + py.stderr)
        self.assertEqual(zig.returncode, py.returncode, detail)
        self.assertIn("Input/output error", py.stderr)
        self.assertIn("Input/output error", zig.stderr)
        self.assertEqual(self._payload(py_root), "old\n")
        self.assertEqual(self._payload(zig_root), self._payload(py_root))
        self.assertEqual(self._journal(py_root), "built")
        self.assertEqual(self._journal(zig_root), self._journal(py_root))


if __name__ == "__main__":
    unittest.main()
