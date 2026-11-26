"""A retry flushes a directory whose earlier flush failed.

The first attempt renames or unlinks, then the parent flush fails. The
journal keeps the obligation. The next attempt flushes the parent again
even though the rename or unlink is already done, and a second failure is
still a failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import unittest

from tests.support import _compile_fuse_faultfs, _fuse_ready, _skip, golden, run_cli, scrub

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faultfs.c")


def _wait_mount(mnt: str, mounted: subprocess.Popen[bytes]) -> None:
    for _ in range(200000):
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            if f" {mnt} " in handle.read():
                return
        if mounted.poll() is not None:
            err = mounted.stderr.read().decode() if mounted.stderr else ""
            raise AssertionError(f"FUSE exited: {err}")
    raise AssertionError("FUSE mount did not appear")


class DirFsyncRetryTests(unittest.TestCase):
    def test_resume_flushes_the_source_parent_again(self) -> None:
        if os.geteuid() == 0:
            _skip(self, "the filesystem is mounted by an unprivileged user")
        reason = _fuse_ready()
        if reason:
            _skip(self, reason)
        if not os.environ.get("TOOL_BIN"):
            _skip(self, "TOOL_BIN is unset")
        work = f"/tmp/stage-fsync-retry-{os.getpid()}"
        done = self._resume(work)
        source_suffix = "/generations/r1/software/demo"
        detail = done["stdout"] + done["stderr"]
        self.assertEqual(done["first"], 2, detail)
        self.assertEqual(done["resume"], 2, detail)
        self.assertIn(source_suffix, done["stderr"])
        self.assertIn(b'"state": "built"', done["journal"])
        self.assertNotIn(b'"state": "complete"', done["journal"])
        self.assertEqual(done["payload"], "old\n", detail)
        golden(self, "resume_stdout", scrub(done["resume_stdout"], WORK=work))
        golden(self, "resume_stderr", scrub(done["resume_stderr"], WORK=work))

    def _resume(self, work: str) -> dict:
        mnt = os.path.join(work, "mnt")
        if os.path.isdir(mnt) and os.path.ismount(mnt):
            subprocess.run(["fusermount3", "-u", mnt], check=False, timeout=30)
        shutil.rmtree(work, ignore_errors=True)
        back = os.path.join(work, "back")
        os.makedirs(os.path.join(back, "software", "demo", "1"))
        os.makedirs(os.path.join(back, "modules", "all", "demo"))
        with open(os.path.join(back, "software", "demo", "1", "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("old\n")
        eb = os.path.join(work, "fake-eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    for arg in "$@"; do
                      case "$arg" in
                        --installpath-software=*) sw="${arg#*=}" ;;
                        --installpath-modules=*) mods="${arg#*=}" ;;
                      esac
                    done
                    mkdir -p "$sw/demo/1" "$mods/all/demo"
                    printf 'new\\n' > "$sw/demo/1/PAYLOAD"
                    printf 'local root = "%s/demo/1"\\n' "$sw" > "$mods/all/demo/1.lua"
                    """
                )
            )
        os.chmod(eb, 0o755)
        binary = os.path.join(work, "faultfs")
        _compile_fuse_faultfs(_SRC, binary)
        os.makedirs(mnt)
        env = dict(os.environ)
        env["FAULTFS_FSYNC_DIR"] = "/generations/r1/software/demo"
        env.pop("FAULTFS_FSYNC_DIR_SKIP", None)
        mounted = subprocess.Popen(
            [binary, back, mnt],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        first = None
        resumed = None
        journal = b""
        try:
            _wait_mount(mnt, mounted)
            with open(os.path.join(mnt, "modules", "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
                handle.write(f'local root = "{mnt}/software/demo/1"\n')
            args = (
                "run",
                "--software",
                os.path.join(mnt, "software"),
                "--modules",
                os.path.join(mnt, "modules"),
                "--state-root",
                mnt,
                "--backend",
                "installpath",
                "--eb",
                eb,
                "--module",
                "demo/1",
                "--run-id",
                "r1",
                "--",
                "demo-1.eb",
            )
            first = run_cli(*args)
            resumed = run_cli("resume", "--state-root", mnt, "r1")
            with open(os.path.join(mnt, "runs", "r1", "journal.json"), "rb") as handle:
                journal = handle.read()
        finally:
            subprocess.run(["fusermount3", "-u", mnt], check=False, timeout=30)
            if mounted.stderr is not None and not mounted.stderr.closed:
                mounted.stderr.close()
            mounted.wait(timeout=30)
        with open(os.path.join(back, "software", "demo", "1", "PAYLOAD"), encoding="utf-8") as handle:
            payload = handle.read()
        assert first is not None and resumed is not None
        return {
            "first": first.returncode,
            "resume": resumed.returncode,
            "stdout": resumed.stdout,
            "stderr": resumed.stderr,
            "resume_stdout": resumed.stdout,
            "resume_stderr": resumed.stderr,
            "journal": journal,
            "payload": payload,
        }
