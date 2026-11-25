"""A failed read of the previous live module file is an operating-system error."""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests.support import _program, _python314, _skip, classic_install

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FAKE = os.path.join(_REPO, "tests", "fake_eb.py")


def _scrub(text, root):
    return text.replace(root, "ROOT")


class LiveModuleReadOsErrorTest(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("TOOL_BIN"):
            _skip(self, "TOOL_BIN is unset")
        self.python = _python314()
        if self.python is None:
            _skip(self, "python3.14 is not installed")
        self.program = _program(self, "eb-fs-fail-live")

    def _layout(self):
        root = tempfile.mkdtemp(prefix="live-read-")
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
        return root, state, args, env

    def test_open_of_the_live_module_is_reported(self):
        tool = os.environ["TOOL_BIN"]
        # The fourth open whose path contains the live module file is the
        # read of its previous bytes. run_cli cannot do this: the tracer
        # must be the parent of the tool.
        py_root, py_state, py_args, py_env = self._layout()
        zig_root, zig_state, zig_args, zig_env = self._layout()
        needle = "modules/all/demo/1.lua"
        py = subprocess.run(
            [self.program, "5", "open", "4", needle, "--", self.python, "-m", "eb_staged_rebuild.cli", *py_args],
            cwd=py_root,
            env=py_env,
            capture_output=True,
            text=True,
            check=False,
        )
        zig = subprocess.run(
            [self.program, "5", "open", "4", needle, "--", tool, *zig_args],
            cwd=zig_root,
            env=zig_env,
            capture_output=True,
            text=True,
            check=False,
        )
        py_err = _scrub(py.stderr, py_root)
        zig_err = _scrub(zig.stderr, zig_root)
        self.assertEqual(py.returncode, zig.returncode, py_err + "\n---\n" + zig_err)
        self.assertEqual(py_err, zig_err)
        self.assertIn("[Errno 5] Input/output error", zig_err)
        with open(os.path.join(py_state, "runs", "run1", "journal.json"), encoding="utf-8") as handle:
            py_state_name = json.load(handle)["state"]
        with open(os.path.join(zig_state, "runs", "run1", "journal.json"), encoding="utf-8") as handle:
            zig_state_name = json.load(handle)["state"]
        self.assertEqual(py_state_name, zig_state_name)
        with open(os.path.join(py_root, "software", "demo", "1", "PAYLOAD"), encoding="utf-8") as handle:
            py_body = handle.read()
        with open(os.path.join(zig_root, "software", "demo", "1", "PAYLOAD"), encoding="utf-8") as handle:
            zig_body = handle.read()
        self.assertEqual(py_body, zig_body)
        self.assertEqual(py_body, "old\n")
