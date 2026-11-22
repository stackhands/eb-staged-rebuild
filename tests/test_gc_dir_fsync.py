"""A failed directory flush stops gc from recording a generation as removed.

Python raises when the flush after the rmdir fails. The binary used to
ignore that flush and save the journal as if the generation were gone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from types import SimpleNamespace

from eb_staged_rebuild.atomicio import read_bytes
from tests.support import run_cli, run_cli_optional_binary
from tests.test_prefix_dir_fsync import _compile, _fuse_ready, _skip


def _wait_mount(mnt: str, mounted: subprocess.Popen[bytes]) -> None:
    for _ in range(200000):
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            if f" {mnt} " in handle.read():
                return
        if mounted.poll() is not None:
            err = mounted.stderr.read().decode() if mounted.stderr else ""
            raise AssertionError(f"FUSE exited: {err}")
    raise AssertionError("FUSE mount did not appear")


class GenerationDirFsyncTests(unittest.TestCase):
    def test_failed_directory_flush_after_generation_removal_is_reported(self) -> None:
        if os.geteuid() == 0:
            _skip(self, "the filesystem is mounted by an unprivileged user")
        reason = _fuse_ready()
        if reason:
            _skip(self, reason)
        if not os.environ.get("TOOL_BIN"):
            _skip(self, "TOOL_BIN is unset")
        python = self._run("python")
        binary = self._run("binary")
        for done in (python, binary):
            detail = done.stdout + done.stderr
            self.assertEqual(done.returncode, 2, detail)
            self.assertIn("Input/output error", detail)

    def test_generation_flush_error_matches_across_frontends(self) -> None:
        """stdout, stderr, status and the journal are the same from either frontend."""
        if os.geteuid() == 0:
            _skip(self, "the filesystem is mounted by an unprivileged user")
        reason = _fuse_ready()
        if reason:
            _skip(self, reason)
        if not os.environ.get("TOOL_BIN"):
            _skip(self, "TOOL_BIN is unset")
        work = f"/tmp/gc-dir-fsync-parity-{os.getpid()}"
        python = self._run("python", work)
        binary = self._run("binary", work)
        self.assertEqual(python.returncode, binary.returncode)
        self.assertEqual(python.stdout, binary.stdout)
        self.assertEqual(python.stderr, binary.stderr)
        self.assertEqual(python.journal, binary.journal)
        generations = os.path.join(work, "mnt", "generations")
        self.assertEqual(python.returncode, 2)
        self.assertEqual(python.stdout, "")
        self.assertEqual(
            python.stderr,
            f"eb-staged-rebuild: [Errno 5] Input/output error: '{generations}'\n",
        )
        self.assertIsInstance(python.journal, bytes)
        self.assertNotIn(b"generation_removed_at", python.journal)

    def _run(self, which: str, work: str | None = None) -> SimpleNamespace:
        if work is None:
            work = f"/tmp/gc-dir-fsync-{os.getpid()}-{which}"
        mnt = os.path.join(work, "mnt")
        if os.path.isdir(mnt) and os.path.ismount(mnt):
            subprocess.run(["fusermount3", "-u", mnt], check=False, timeout=30)
        shutil.rmtree(work, ignore_errors=True)
        back = os.path.join(work, "back")
        os.makedirs(os.path.join(back, "generations", "r1"))
        with open(os.path.join(back, "generations", "r1", "note"), "w", encoding="utf-8") as handle:
            handle.write("keep\n")
        os.makedirs(os.path.join(back, "runs", "r1"))
        os.makedirs(os.path.join(back, "software", "demo"))
        binary = os.path.join(work, "faultfs")
        _compile(binary)
        os.makedirs(mnt)
        env = dict(os.environ)
        env["FAULTFS_FSYNC_DIR"] = "/generations"
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
        journal_bytes = b""
        try:
            _wait_mount(mnt, mounted)
            live = os.path.join(mnt, "software", "demo", "1")
            retired = os.path.join(mnt, "software", "demo", ".1.old")
            journal = {
                "schema": 2,
                "publication": "directory-swap",
                "run_id": "r1",
                "state": "complete",
                "grace_hours": 0,
                "created_at": 1,
                "updated_at": 1,
                "modules_plan": [
                    {
                        "rel": "demo/1",
                        "live_software": live,
                        "retired": retired,
                        "published_at": 1,
                        "status": "published",
                    }
                ],
            }
            with open(os.path.join(mnt, "runs", "r1", "journal.json"), "w", encoding="utf-8") as handle:
                json.dump(journal, handle)
            args = ("gc", "--state-root", mnt, "--grace-hours", "0")
            if which == "python":
                done = run_cli(*args)
            else:
                done = run_cli_optional_binary(*args)
                if done is None:
                    self.fail("TOOL_BIN is unset")
            journal_path = os.path.join(mnt, "runs", "r1", "journal.json")
            journal_bytes = read_bytes(journal_path)
            with open(journal_path, "rb") as handle:
                if handle.read() != journal_bytes:
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
            self.fail("gc did not run")
        return SimpleNamespace(
            returncode=done.returncode,
            stdout=done.stdout,
            stderr=done.stderr,
            journal=journal_bytes,
        )
