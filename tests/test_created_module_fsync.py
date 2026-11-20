"""A failed directory flush after removing a created module stops rollback.

Rollback removes a module file the run created and then flushes that
file's directory. Python raises and puts the published tree back. The
binary ignored the flush and recorded the rollback. The tracer has to
be the parent of the release binary.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

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


class CreatedModuleFsyncTest(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("TOOL_BIN"):
            self.skipTest("TOOL_BIN is unset")
        self.python = _python314()
        if self.python is None:
            self.skipTest("python3.14 is not installed")

    def _layout(self):
        root = tempfile.mkdtemp(prefix="created-fsync-")
        self.addCleanup(shutil.rmtree, root, True)
        software = os.path.join(root, "software", "demo", "1", "easybuild")
        os.makedirs(software)
        os.makedirs(os.path.join(root, "modules"))
        with open(os.path.join(root, "software", "demo", "1", "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("old\n")
        with open(os.path.join(software, "demo-1.eb"), "w", encoding="utf-8") as handle:
            handle.write("name = 'demo'\n")
        eb = os.path.join(root, "eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/sh\nexec {self.python} {_FAKE} \"$@\"\n")
        os.chmod(eb, stat.S_IRWXU)
        run_args = [
            "run",
            "--software",
            os.path.join(root, "software"),
            "--modules",
            os.path.join(root, "modules"),
            "--state-root",
            os.path.join(root, "state"),
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
        back_args = ["rollback", "--state-root", os.path.join(root, "state"), "--lock-timeout", "0", "run1"]
        env = {
            "PYTHONPATH": _REPO,
            "PYTHONIOENCODING": "utf-8:strict",
            "LANG": "C.UTF-8",
            "FAKE_EB_VERSION": "5.4.2",
            "FAKE_EB_PAYLOAD": "new\n",
            "FAKE_EB_REL": "demo/1",
            "PATH": os.environ.get("PATH", ""),
        }
        return root, env, run_args, back_args

    def _publish(self, argv, root, env, run_args):
        done = subprocess.run(argv + run_args, cwd=root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_failed_directory_flush_keeps_the_created_module(self):
        # The first fsync under modules/all/demo is the directory flush after
        # the module file this run created is removed.
        tool = os.environ["TOOL_BIN"]
        program = _program()
        py_root, py_env, py_run, py_back = self._layout()
        zig_root, zig_env, zig_run, zig_back = self._layout()
        self._publish([self.python, "-m", "eb_staged_rebuild.cli"], py_root, py_env, py_run)
        self._publish([tool], zig_root, zig_env, zig_run)
        py = subprocess.run(
            [program, "5", "fsync", "1", "modules/all/demo", "--", self.python, "-m", "eb_staged_rebuild.cli", *py_back],
            cwd=py_root,
            env=py_env,
            capture_output=True,
            text=True,
            check=False,
        )
        zig = subprocess.run(
            [program, "5", "fsync", "1", "modules/all/demo", "--", tool, *zig_back],
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
        self.assertEqual(self._payload(py_root), "new\n")
        self.assertEqual(self._payload(zig_root), self._payload(py_root))
        self.assertTrue(os.path.isfile(os.path.join(py_root, "modules", "all", "demo", "1.lua")))
        self.assertEqual(
            os.path.isfile(os.path.join(zig_root, "modules", "all", "demo", "1.lua")),
            os.path.isfile(os.path.join(py_root, "modules", "all", "demo", "1.lua")),
        )
        self.assertEqual(self._journal(py_root), "complete")
        self.assertEqual(self._journal(zig_root), self._journal(py_root))

    def _payload(self, root):
        with open(os.path.join(root, "software", "demo", "1", "PAYLOAD"), encoding="utf-8") as handle:
            return handle.read()

    def _journal(self, root):
        with open(os.path.join(root, "state", "runs", "run1", "journal.json"), encoding="utf-8") as handle:
            return json.load(handle).get("state")


if __name__ == "__main__":
    unittest.main()
