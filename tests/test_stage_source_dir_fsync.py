"""A cross-directory rename of a staged tree flushes both parents.

The staged install moves from its generation directory to the retired
name under the live software tree. The source parent has to be flushed
for the removal of the old name to be durable, and the destination
parent for the new name. The destination was flushed. The source was
not, so a flush error on the source parent used to be ignored and the
run published.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import unittest
from types import SimpleNamespace

from eb_staged_rebuild.atomicio import read_bytes
from tests.support import _run, run_cli, run_cli_optional_binary
from tests.test_prefix_dir_fsync import _compile, _fuse_ready, _skip

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NOW = "1700000000.25"


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
        python = self._run("python", work)
        binary = self._run("binary", work)
        source = os.path.join(work, "mnt", "generations", "r1", "software", "demo")
        for done in (python, binary):
            detail = done.stdout + done.stderr
            self.assertEqual(done.returncode, 2, detail)
            self.assertIn(f"Input/output error: '{source}'", done.stderr)
            self.assertEqual(done.payload, "old\n", detail)
        self.assertEqual(python.returncode, binary.returncode)
        self.assertEqual(python.stdout, binary.stdout)
        self.assertEqual(python.stderr, binary.stderr)

    def test_journals_are_the_same_bytes_under_the_test_clock(self) -> None:
        if os.geteuid() == 0:
            _skip(self, "the filesystem is mounted by an unprivileged user")
        reason = _fuse_ready()
        if reason:
            _skip(self, reason)
        if not os.environ.get("TOOL_BIN_TEST_OPS"):
            _skip(self, "TOOL_BIN_TEST_OPS is unset")
        work = f"/tmp/stage-source-fsync-bytes-{os.getpid()}"
        python = self._run("python", work, clock=True)
        binary = self._run("binary", work, clock=True)
        self.assertEqual(python.returncode, 2, python.stdout + python.stderr)
        self.assertEqual(binary.returncode, 2, binary.stdout + binary.stderr)
        self.assertEqual(python.stdout, binary.stdout)
        self.assertEqual(python.stderr, binary.stderr)
        self.assertIsInstance(python.journal, bytes)
        self.assertEqual(python.journal, binary.journal)
        self.assertIn(b'"created_at": 1700000000.25', python.journal)
        self.assertIn(b'"updated_at": 1700000000.25', python.journal)

    def _run(self, which: str, work: str, clock: bool = False) -> SimpleNamespace:
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
        _compile(binary)
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
                cli_env = {"EB_STAGED_REBUILD_NOW": _NOW}
                if which == "python":
                    done = _run([sys.executable, os.path.join(_REPO, "tests", "now_cli.py"), *args], cli_env)
                else:
                    tool = os.environ.get("TOOL_BIN_TEST_OPS")
                    if not tool:
                        self.fail("TOOL_BIN_TEST_OPS is unset")
                    done = _run([tool, *args], cli_env)
            elif which == "python":
                done = run_cli(*args)
            else:
                done = run_cli_optional_binary(*args)
                if done is None:
                    self.fail("TOOL_BIN is unset")
            journal_path = os.path.join(mnt, "runs", "r1", "journal.json")
            if os.path.isfile(journal_path):
                journal = read_bytes(journal_path)
                with open(journal_path, "rb") as handle:
                    if handle.read() != journal:
                        self.fail("read_bytes and an rb read disagree")
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
