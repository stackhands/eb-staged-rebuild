"""A failed read of the previous live module file is an operating-system error."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from tests.support import _program, _skip, classic_install, golden, scrub

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FAKE = os.path.join(_REPO, "tests", "fake_eb.py")


class LiveModuleReadOsErrorTest(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("TOOL_BIN"):
            _skip(self, "TOOL_BIN is unset")
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
            handle.write(f'#!/bin/sh\nexec "{sys.executable}" "{_FAKE}" "$@"\n')
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
        root, state, args, env = self._layout()
        needle = "modules/all/demo/1.lua"
        zig = subprocess.run(
            [self.program, "5", "open", "4", needle, "--", tool, *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        golden(self, "returncode", f"{zig.returncode}\n")
        golden(self, "stderr", scrub(zig.stderr, ROOT=root))
        self.assertIn("[Errno 5] Input/output error", zig.stderr)
        with open(os.path.join(state, "runs", "run1", "journal.json"), encoding="utf-8") as handle:
            state_name = json.load(handle)["state"]
        golden(self, "journal-state", f"{state_name}\n")
        with open(os.path.join(root, "software", "demo", "1", "PAYLOAD"), encoding="utf-8") as handle:
            body = handle.read()
        self.assertEqual(body, "old\n")
