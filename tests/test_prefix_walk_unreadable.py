"""A directory the owner cannot list aborts the prefix rewrite.

``os.walk`` ignores a directory it cannot list unless ``onerror`` raises.
The release binary refuses that directory. This test builds a mode 000
directory after the stand-in install. The Python command is checked on
every run, and the binary when that program is set.
"""

import os
import shutil
import stat
import sys
import tempfile
import unittest

from tests.support import classic_install, run_cli, run_cli_optional_binary

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FAKE = os.path.join(_REPO, "tests", "fake_eb.py")


def _release(root):
    for dirpath, dirnames, _filenames in os.walk(root):
        for name in dirnames:
            path = os.path.join(dirpath, name)
            try:
                mode = os.lstat(path).st_mode
            except OSError:
                continue
            if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
                os.chmod(path, stat.S_IRWXU)
    shutil.rmtree(root)


class PrefixWalkUnreadableTest(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("the directory mode is ignored when the effective uid is 0")

    def _layout(self):
        root = tempfile.mkdtemp(prefix="prefix-walk-")
        self.addCleanup(_release, root)
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        os.makedirs(software)
        os.makedirs(modules)
        classic_install(software, modules, "demo/1", "old\n", "all/demo/1.lua")
        wrapper = os.path.join(root, "eb")
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/bin/sh\n"
                f'"{sys.executable}" "{_FAKE}" "$@" || exit $?\n'
                "software=\n"
                'for arg in "$@"; do\n'
                '  case "$arg" in\n'
                "    --installpath-software=*) software=${arg#--installpath-software=} ;;\n"
                "  esac\n"
                "done\n"
                'hidden="$software/demo/1/hidden"\n'
                'mkdir -p "$hidden"\n'
                'chmod 000 "$hidden"\n'
            )
        os.chmod(wrapper, stat.S_IRWXU)
        args = [
            "run",
            "--software", software,
            "--modules", modules,
            "--state-root", state,
            "--backend", "installpath",
            "--eb", wrapper,
            "--run-id", "run1",
            "--skip-live-check",
            "--lock-timeout", "0",
            "--module", "demo/1",
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
            "HOME": os.environ.get("HOME", ""),
        }
        return root, state, os.path.join(state, "generations", "run1", "software", "demo", "1", "hidden"), args, env

    def test_an_unreadable_directory_refuses_the_rewrite(self):
        py_root, py_state, py_hidden, py_args, py_env = self._layout()
        python = run_cli(*py_args, env=py_env, cwd=py_root)
        self.assertNotEqual(python.returncode, 0)
        self.assertIn("Permission denied", python.stderr)
        self.assertIn(py_hidden, python.stderr)
        self.assertFalse(os.access(py_hidden, os.R_OK))
        zig_root, zig_state, zig_hidden, zig_args, zig_env = self._layout()
        binary = run_cli_optional_binary(*zig_args, env=zig_env, cwd=zig_root)
        if binary is None:
            return
        self.assertEqual(python.returncode, binary.returncode, (python.stderr, binary.stderr))
        self.assertEqual(
            python.stderr.replace(py_state, "STATE"),
            binary.stderr.replace(zig_state, "STATE"),
        )
        self.assertIn(zig_hidden, binary.stderr)
        self.assertFalse(os.access(zig_hidden, os.R_OK))
