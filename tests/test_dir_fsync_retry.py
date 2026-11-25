"""A retry flushes a directory whose earlier flush failed.

The first attempt renames or unlinks, then the parent flush fails. The
journal keeps the obligation. The next attempt used to see the rename
or unlink already done and report success without flushing. It flushes
again, and a second failure is still a failure.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import textwrap
import unittest

from eb_staged_rebuild.atomicio import read_bytes
from eb_staged_rebuild.engine import _remove_created_links
from tests.support import _compile_fuse_faultfs, _fuse_ready, _skip, run_cli, run_cli_optional_binary

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
        python = self._resume("python", work)
        binary = self._resume("binary", work)
        source_suffix = "/generations/r1/software/demo"
        for done in (python, binary):
            detail = done["stdout"] + done["stderr"]
            self.assertEqual(done["first"], 2, detail)
            self.assertEqual(done["resume"], 2, detail)
            self.assertIn(source_suffix, done["stderr"])
            self.assertIn(b'"state": "built"', done["journal"])
            self.assertNotIn(b'"state": "complete"', done["journal"])
            self.assertEqual(done["payload"], "old\n", detail)
        self.assertEqual(python["resume_stdout"], binary["resume_stdout"])
        self.assertEqual(python["resume_stderr"], binary["resume_stderr"])

    def test_a_missing_created_link_is_flushed_again(self) -> None:
        if os.geteuid() == 0:
            _skip(self, "the filesystem is mounted by an unprivileged user")
        reason = _fuse_ready()
        if reason:
            _skip(self, reason)
        work = f"/tmp/link-fsync-retry-{os.getpid()}"
        mnt = os.path.join(work, "mnt")
        back = os.path.join(work, "back")
        os.makedirs(os.path.join(back, "links"))
        binary = os.path.join(work, "faultfs")
        _compile_fuse_faultfs(_SRC, binary)
        os.makedirs(mnt)
        env = dict(os.environ)
        env["FAULTFS_FSYNC_DIR"] = "/links"
        env.pop("FAULTFS_FSYNC_DIR_SKIP", None)
        mounted = subprocess.Popen(
            [binary, back, mnt],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            _wait_mount(mnt, mounted)
            parent = os.path.join(mnt, "links")
            path = os.path.join(parent, "a.lua")
            os.symlink("target", path)
            journal = {
                "links_plan": [
                    {
                        "rel": "links/a.lua",
                        "live_path": path,
                        "target": "target",
                        "created": True,
                        "status": "created",
                    }
                ]
            }
            for _ in range(2):
                with self.assertRaises(OSError) as caught:
                    _remove_created_links(journal)
                self.assertEqual(caught.exception.errno, errno.EIO)
                self.assertEqual(caught.exception.filename, parent)
                self.assertTrue(journal["links_plan"][0]["created"])
            self.assertFalse(os.path.lexists(path))
        finally:
            subprocess.run(["fusermount3", "-u", mnt], check=False, timeout=30)
            if mounted.stderr is not None and not mounted.stderr.closed:
                mounted.stderr.close()
            mounted.wait(timeout=30)
            shutil.rmtree(work, ignore_errors=True)

    def _resume(self, which: str, work: str) -> dict:
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
            if which == "python":
                first = run_cli(*args)
                resumed = run_cli("resume", "--state-root", mnt, "r1")
            else:
                first = run_cli_optional_binary(*args)
                resumed = run_cli_optional_binary("resume", "--state-root", mnt, "r1")
                if first is None or resumed is None:
                    self.fail("TOOL_BIN is unset")
            journal_path = os.path.join(mnt, "runs", "r1", "journal.json")
            journal = read_bytes(journal_path)
            with open(journal_path, "rb") as handle:
                if handle.read() != journal:
                    self.fail("read_bytes and an rb read disagree")
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
