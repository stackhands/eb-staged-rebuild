"""A failed unlink during rollback is an OSError, and the published tree stays."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from tests.support import classic_install


def _python314():
    if sys.version_info[:2] == (3, 14):
        return sys.executable
    import shutil

    found = shutil.which("python3.14")
    if not found:
        return None
    completed = subprocess.run(
        [found, "-c", "import sys; raise SystemExit(0 if sys.version_info[:2]==(3,14) else 1)"],
        check=False,
    )
    return found if completed.returncode == 0 else None

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FAKE = os.path.join(_REPO, "tests", "fake_eb.py")


def _program():
    """Build tests/fs_fail.c. The release binary is static, so the tracer has to be its parent."""
    src = os.path.join(_REPO, "tests", "fs_fail.c")
    out = os.path.join(tempfile.gettempdir(), "eb-fs-fail")
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(src):
        subprocess.run(["gcc", "-O2", "-o", out, src], check=True)
    return out


def _scrub(text, root):
    return text.replace(root, "ROOT")


def _payload(root):
    path = os.path.join(root, "software", "demo", "1", "PAYLOAD")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _state(root):
    path = os.path.join(root, "state", "runs", "run1", "journal.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle).get("state")


class RollbackUnlinkErrorTest(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("TOOL_BIN"):
            self.skipTest("TOOL_BIN is unset")
        self.python = _python314()
        if self.python is None:
            self.skipTest("python3.14 is not installed")

    def _layout(self):
        root = tempfile.mkdtemp(prefix="rb-unlink-")
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
        run_args = [
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
        back_args = ["rollback", "--state-root", state, "--lock-timeout", "0", "run1"]
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

    def _traced(self, argv, root, env, errno, op, nth, needle):
        # run_cli cannot do this: the tracer must be the parent of the tool.
        return subprocess.run(
            [_program(), str(errno), op, str(nth), needle, "--", *argv],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _publish(self, argv, root, env, run_args):
        done = subprocess.run(argv + run_args, cwd=root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)

    def _compare(self, errno, op, nth, needle):
        tool = os.environ["TOOL_BIN"]
        py_root, py_env, py_run, py_back = self._layout()
        zig_root, zig_env, zig_run, zig_back = self._layout()
        self._publish([self.python, "-m", "eb_staged_rebuild.cli"], py_root, py_env, py_run)
        self._publish([tool], zig_root, zig_env, zig_run)
        py = self._traced(
            [self.python, "-m", "eb_staged_rebuild.cli", *py_back], py_root, py_env, errno, op, nth, needle
        )
        zig = self._traced([tool, *zig_back], zig_root, zig_env, errno, op, nth, needle)
        py_err = _scrub(py.stderr, py_root)
        zig_err = _scrub(zig.stderr, zig_root)
        self.assertEqual(py.returncode, zig.returncode, py_err + "\n---\n" + zig_err)
        self.assertEqual(py_err, zig_err)
        self.assertEqual(_payload(py_root), _payload(zig_root))
        self.assertEqual(_state(py_root), _state(zig_root))

    def test_unlink_eio_keeps_the_published_tree(self):
        self._compare(5, "unlink", 1, "modules/tools/demo/1.lua")

    def test_link_dir_fsync_eio_keeps_the_published_tree(self):
        self._compare(5, "fsync", 1, "modules/tools/demo")
