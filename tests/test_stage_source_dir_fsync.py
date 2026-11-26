"""A cross-directory rename of a staged tree flushes both parents.

The staged install moves from its generation directory to the retired
name under the live software tree. The source parent has to be flushed
for the removal of the old name to be durable, and the destination
parent for the new name. A flush error on the source parent fails the run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
import unittest
from types import SimpleNamespace

from tests.support import _compile_fuse_faultfs, _fuse_ready, _run, _skip, golden, run_cli, scrub

_NOW = "1700000000.25"
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


class StageSourceDirFsyncTests(unittest.TestCase):
    def test_failed_source_parent_flush_matches_across_frontends(self) -> None:
        if os.geteuid() == 0:
            _skip(self, "the filesystem is mounted by an unprivileged user")
        reason = _fuse_ready()
        if reason:
            _skip(self, reason)
        if not os.environ.get("TOOL_BIN"):
            _skip(self, "TOOL_BIN is unset")
        work = f"/tmp/stage-source-fsync-{os.getpid()}"
        done = self._run(work)
        source = os.path.join(work, "mnt", "generations", "r1", "software", "demo")
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, detail)
        self.assertIn(f"Input/output error: '{source}'", done.stderr)
        self.assertEqual(done.payload, "old\n", detail)
        golden(self, "stdout", scrub(done.stdout, WORK=work))
        golden(self, "stderr", scrub(done.stderr, WORK=work))

    def test_journals_are_the_same_bytes_under_the_test_clock(self) -> None:
        if os.geteuid() == 0:
            _skip(self, "the filesystem is mounted by an unprivileged user")
        reason = _fuse_ready()
        if reason:
            _skip(self, reason)
        if not os.environ.get("TOOL_BIN_TEST_OPS"):
            _skip(self, "TOOL_BIN_TEST_OPS is unset")
        work = f"/tmp/stage-source-fsync-bytes-{os.getpid()}"
        done = self._run(work, clock=True)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        golden(self, "stdout", scrub(done.stdout, WORK=work))
        golden(self, "stderr", scrub(done.stderr, WORK=work))
        self.assertIsInstance(done.journal, bytes)
        journal = scrub(done.journal.decode("utf-8", "surrogateescape"), WORK=work)
        # The module file's hash covers the work directory's name, which holds the pid.
        journal = re.sub(r'"((?:new|old)_hash)": "[0-9a-f]{64}"', r'"\1": "HASH"', journal)
        golden(self, "journal", journal)
        self.assertIn(b'"created_at": 1700000000.25', done.journal)
        self.assertIn(b'"updated_at": 1700000000.25', done.journal)

    def _run(self, work: str, clock: bool = False) -> SimpleNamespace:
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
        env.pop("FAULTFS_FALLOCATE", None)
        env.pop("FAULTFS_SEAL", None)
        mounted = subprocess.Popen(
            [binary, back, mnt],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        done: subprocess.CompletedProcess[str] | None = None
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
            if clock:
                tool = os.environ.get("TOOL_BIN_TEST_OPS")
                if not tool:
                    self.fail("TOOL_BIN_TEST_OPS is unset")
                done = _run([tool, *args], {"EB_STAGED_REBUILD_NOW": _NOW})
            else:
                done = run_cli(*args)
            journal_path = os.path.join(mnt, "runs", "r1", "journal.json")
            if os.path.isfile(journal_path):
                with open(journal_path, "rb") as handle:
                    journal = handle.read()
        finally:
            subprocess.run(["fusermount3", "-u", mnt], check=False, timeout=30)
            if mounted.stderr is not None and not mounted.stderr.closed:
                mounted.stderr.close()
            for _ in range(200000):
                if mounted.poll() is not None:
                    break
            else:
                mounted.kill()
                mounted.wait()
        if done is None:
            self.fail("run did not run")
        with open(os.path.join(back, "software", "demo", "1", "PAYLOAD"), encoding="utf-8") as handle:
            payload = handle.read()
        return SimpleNamespace(
            returncode=done.returncode,
            stdout=done.stdout,
            stderr=done.stderr,
            payload=payload,
            journal=journal,
        )
