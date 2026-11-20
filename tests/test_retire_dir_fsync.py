"""A directory flush that fails after a retire rename stops the command.

The module file is renamed into the state root, and the directories of
that rename are flushed afterwards. Python raises. The binary ignored
the flushes, recorded the retire, and exited 0.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faultfs.c")


def _skip(case: unittest.TestCase, reason: str) -> None:
    if os.environ.get("EB_STAGED_REBUILD_REQUIRE_TESTS") == "1":
        case.fail(reason)
    case.skipTest(reason)


def _fuse_ready() -> str | None:
    if not os.path.exists("/dev/fuse"):
        return "FUSE is not available"
    if shutil.which("fusermount3") is None or shutil.which("cc") is None:
        return "FUSE is not available"
    flags = subprocess.run(
        ["pkg-config", "--cflags", "--libs", "fuse3"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if flags.returncode != 0:
        return "FUSE is not available"
    return None


def _compile(binary: str) -> None:
    flags = subprocess.run(
        ["pkg-config", "--cflags", "--libs", "fuse3"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    done = subprocess.run(
        ["cc", "-O2", "-Wall", "-Wextra", "-o", binary, _SRC, *flags.stdout.split()],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if done.returncode != 0:
        raise AssertionError(done.stderr)


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
        python = self._run("python")
        binary = self._run(tool)
        detail_py = python.stdout + python.stderr
        detail_bin = binary.stdout + binary.stderr
        self.assertEqual(python.returncode, 2, detail_py)
        self.assertEqual(binary.returncode, 2, detail_bin)
        self.assertIn("Input/output error", detail_py)
        self.assertIn("Input/output error", detail_bin)
        self.assertIn("JOURNAL retiring", detail_py)
        self.assertIn("JOURNAL retiring", detail_bin)
        self.assertIn("LIVE absent", detail_py)
        self.assertIn("LIVE absent", detail_bin)
        self.assertNotIn("left in place", detail_py)
        self.assertNotIn("left in place", detail_bin)

    def _run(self, tool: str) -> subprocess.CompletedProcess[str]:
        label = "python" if tool == "python" else "zig"
        work = f"/tmp/retire-dir-fsync-{os.getpid()}-{label}"
        mnt = os.path.join(work, "mnt")
        if os.path.isdir(mnt) and os.path.ismount(mnt):
            subprocess.run(["fusermount3", "-u", mnt], check=False, timeout=30)
        shutil.rmtree(work, ignore_errors=True)
        back = os.path.join(work, "back")
        os.makedirs(os.path.join(back, "software", "demo", "1"))
        os.makedirs(os.path.join(back, "modules", "all", "demo"))
        os.makedirs(os.path.join(back, "state"))
        binary = os.path.join(work, "faultfs")
        _compile(binary)
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
            self._wait_mount(mnt, mounted)
            install = os.path.join(mnt, "software", "demo", "1")
            module = os.path.join(mnt, "modules", "all", "demo", "1.lua")
            with open(module, "w", encoding="utf-8") as handle:
                handle.write(f'local root = "{install}"\n')
            with open(os.path.join(install, "PAYLOAD"), "w", encoding="utf-8") as handle:
                handle.write("old\n")
            state = os.path.join(mnt, "state")
            run_env = dict(os.environ)
            run_env["PYTHONPATH"] = _REPO
            run_env["PYTHONIOENCODING"] = "utf-8:strict"
            argv = [sys.executable, "-m", "eb_staged_rebuild"] if tool == "python" else [tool]
            done = subprocess.run(
                [*argv, "retire", "--state-root", state, "--module-file", module],
                check=False,
                capture_output=True,
                env=run_env,
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

    def _wait_mount(self, mnt: str, mounted: subprocess.Popen[bytes]) -> None:
        for _ in range(50):
            with open("/proc/self/mountinfo", encoding="utf-8") as handle:
                if f" {mnt} " in handle.read():
                    return
            if mounted.poll() is not None:
                err = mounted.stderr.read().decode() if mounted.stderr else ""
                self.fail(f"FUSE exited: {err}")
            time.sleep(0.05)
        self.fail("FUSE mount did not appear")


if __name__ == "__main__":
    unittest.main()
