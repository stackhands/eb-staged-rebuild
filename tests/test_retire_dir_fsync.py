"""A directory flush that fails after a retire rename stops the command.

The module file is renamed into the state root, and the directories of
that rename are flushed afterwards. A failed flush stops the command
before it records the retire.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faultfs.c")
from tests.support import _compile_fuse_faultfs, _fuse_ready, _skip, _wait_mount


class RetireDirFsyncTests(unittest.TestCase):
    def test_failed_directory_flush_leaves_the_retire_unrecorded(self) -> None:
        if os.geteuid() == 0:
            _skip(self, "the filesystem is mounted by an unprivileged user")
        reason = _fuse_ready()
        if reason:
            _skip(self, reason)
        tool = os.environ.get("TOOL_BIN")
        if not tool:
            _skip(self, "TOOL_BIN is unset")
        binary = self._run(tool)
        detail_bin = binary.stdout + binary.stderr
        self.assertEqual(binary.returncode, 2, detail_bin)
        self.assertIn("Input/output error", detail_bin)
        self.assertIn("JOURNAL retiring", detail_bin)
        self.assertIn("LIVE absent", detail_bin)
        self.assertNotIn("left in place", detail_bin)

    def _run(self, tool: str) -> subprocess.CompletedProcess[str]:
        work = f"/tmp/retire-dir-fsync-{os.getpid()}-zig"
        mnt = os.path.join(work, "mnt")
        if os.path.isdir(mnt) and os.path.ismount(mnt):
            subprocess.run(["fusermount3", "-u", mnt], check=False, timeout=30)
        shutil.rmtree(work, ignore_errors=True)
        back = os.path.join(work, "back")
        os.makedirs(os.path.join(back, "software", "demo", "1"))
        os.makedirs(os.path.join(back, "modules", "all", "demo"))
        os.makedirs(os.path.join(back, "state"))
        binary = os.path.join(work, "faultfs")
        _compile_fuse_faultfs(_SRC, binary)
        os.makedirs(mnt)
        env = dict(os.environ)
        env["FAULTFS_FSYNC_DIR"] = "/modules/all/demo"
        env.pop("FAULTFS_FSYNC_DIR_SKIP", None)
        env.pop("FAULTFS_FALLOCATE", None)
        env.pop("FAULTFS_SEAL", None)
        mounted = subprocess.Popen(
            [binary, back, mnt],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            _wait_mount(self, mnt, mounted)
            install = os.path.join(mnt, "software", "demo", "1")
            module = os.path.join(mnt, "modules", "all", "demo", "1.lua")
            with open(module, "w", encoding="utf-8") as handle:
                handle.write(f'local root = "{install}"\n')
            with open(os.path.join(install, "PAYLOAD"), "w", encoding="utf-8") as handle:
                handle.write("old\n")
            state = os.path.join(mnt, "state")
            done = subprocess.run(
                [tool, "retire", "--state-root", state, "--module-file", module],
                check=False,
                capture_output=True,
                timeout=60,
            )
            live = "present" if os.path.lexists(module) else "absent"
            journal_state = "missing"
            runs = os.path.join(state, "runs")
            if os.path.isdir(runs):
                for name in sorted(os.listdir(runs)):
                    path = os.path.join(runs, name, "journal.json")
                    if os.path.isfile(path):
                        with open(path, encoding="utf-8") as handle:
                            journal_state = json.load(handle).get("state", "missing")
        finally:
            subprocess.run(["fusermount3", "-u", mnt], check=False, timeout=30)
            if mounted.stderr is not None and not mounted.stderr.closed:
                mounted.stderr.close()
            try:
                mounted.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mounted.kill()
        stdout = done.stdout.decode("utf-8", "surrogateescape") + f"\nJOURNAL {journal_state}\nLIVE {live}\n"
        stderr = done.stderr.decode("utf-8", "surrogateescape")
        return subprocess.CompletedProcess(done.args, done.returncode, stdout, stderr)

if __name__ == "__main__":
    unittest.main()
