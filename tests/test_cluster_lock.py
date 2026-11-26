import errno
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.lockfile import FileLock


REMOTE_CONTENDER = """
import fcntl, sys
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.lockfile import FileLock
fcntl.flock = lambda *args: None
lock = FileLock(sys.argv[1])
try:
    lock.acquire(0)
except Refused:
    print('excluded')
else:
    lock.release()
    raise AssertionError('record lock failed to exclude another process')
"""


class ClusterLockTests(unittest.TestCase):
    def assert_remote_excluded(self, root):
        result = subprocess.run(
            [sys.executable, "-c", REMOTE_CONTENDER, root],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "excluded")

    def test_record_lock_excludes_an_independent_kernel_gate(self):
        with tempfile.TemporaryDirectory() as root:
            holder = FileLock(root)
            holder.acquire(0)
            try:
                self.assert_remote_excluded(root)
            finally:
                holder.release()
            contender = FileLock(root)
            contender.acquire(0)
            contender.release()

    def test_failed_local_contenders_preserve_record_lock(self):
        with tempfile.TemporaryDirectory() as root:
            holder = FileLock(root)
            holder.acquire(0)
            try:
                for _ in range(4):
                    with self.assertRaises(Refused):
                        FileLock(root).acquire(0)
                self.assert_remote_excluded(root)
            finally:
                holder.release()

    def test_double_acquire_preserves_both_locks(self):
        with tempfile.TemporaryDirectory() as root:
            holder = FileLock(root)
            holder.acquire(0)
            try:
                with self.assertRaises(RuntimeError):
                    holder.acquire(0)
                self.assert_remote_excluded(root)
            finally:
                holder.release()

    def test_acquisition_errors_release_descriptors_and_gate(self):
        with tempfile.TemporaryDirectory() as root:
            for target in ("flock", "lockf", "ftruncate", "write"):
                owner = "fcntl" if target in ("flock", "lockf") else "os"
                lock = FileLock(root)
                before = len(os.listdir("/proc/self/fd"))
                with patch(
                    "eb_staged_rebuild.lockfile." + owner + "." + target,
                    side_effect=OSError(errno.EIO, "injected I/O error"),
                ):
                    with self.assertRaises(OSError):
                        lock.acquire()
                self.assertIsNone(lock.fd)
                self.assertEqual(len(os.listdir("/proc/self/fd")), before)
                other = FileLock(root)
                other.acquire(0)
                other.release()

    def test_sidecar_open_failure_releases_local_gate(self):
        with tempfile.TemporaryDirectory() as root:
            lock = FileLock(root)
            real_open = os.open

            def open_file(path, *args, **kwargs):
                # The sidecar is opened relative to a directory descriptor,
                # so the path is the final component, not the absolute name.
                if path == lock.posix_path or path == ".lock.posix":
                    raise OSError(errno.EIO, "injected sidecar error")
                return real_open(path, *args, **kwargs)

            with patch("eb_staged_rebuild.lockfile.os.open", side_effect=open_file):
                with self.assertRaises(OSError):
                    lock.acquire(0)
            other = FileLock(root)
            other.acquire(0)
            other.release()

    def test_fork_child_release_does_not_unlock_parent(self):
        with tempfile.TemporaryDirectory() as root:
            holder = FileLock(root)
            holder.acquire(0)
            try:
                pid = os.fork()
                if pid == 0:
                    holder.release()
                    os._exit(0)
                _, status = os.waitpid(pid, 0)
                self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                with self.assertRaises(Refused):
                    FileLock(root).acquire(0)
                self.assert_remote_excluded(root)
            finally:
                holder.release()

    def test_release_error_still_closes_both_layers(self):
        with tempfile.TemporaryDirectory() as root:
            holder = FileLock(root)
            holder.acquire(0)
            before = len(os.listdir("/proc/self/fd"))
            with patch(
                "eb_staged_rebuild.lockfile.fcntl.lockf",
                side_effect=OSError(errno.EIO, "injected unlock error"),
            ):
                with self.assertRaises(OSError):
                    holder.release()
            self.assertEqual(len(os.listdir("/proc/self/fd")), before - 2)
            other = FileLock(root)
            other.acquire(0)
            other.release()

    def test_sidecar_hardlink_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            sidecar = Path(root) / ".lock.posix"
            sidecar.touch()
            os.link(sidecar, Path(root) / "sidecar-alias")
            with self.assertRaises(Refused):
                FileLock(root).acquire(0)

    def test_gate_hardlink_is_not_truncated(self):
        with tempfile.TemporaryDirectory() as root:
            payload = Path(root) / "payload"
            payload.write_text("secret")
            os.link(payload, Path(root) / ".lock")
            with self.assertRaises(Refused):
                FileLock(root).acquire(0)
            self.assertEqual(payload.read_text(), "secret")

    def test_gate_fifo_is_refused_without_blocking(self):
        with tempfile.TemporaryDirectory() as root:
            fifo = Path(root) / ".lock"
            os.mkfifo(fifo)
            with self.assertRaises(Refused):
                FileLock(root).acquire(0)
            with self.assertRaises(OSError) as caught:
                os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            self.assertEqual(caught.exception.errno, errno.ENXIO)

    def test_symlink_state_root_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            real = Path(root) / "real"
            real.mkdir()
            link = Path(root) / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(Refused):
                FileLock(str(link)).acquire(0)
            self.assertFalse((real / ".lock").exists())

    def test_lock_files_are_owner_only(self):
        with tempfile.TemporaryDirectory() as root:
            lock = FileLock(root)
            lock.acquire(0)
            try:
                self.assertEqual(stat.S_IMODE((Path(root) / ".lock").stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE((Path(root) / ".lock.posix").stat().st_mode), 0o600)
            finally:
                lock.release()

    def test_nan_timeout_does_not_hang_on_contended_lock(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as root:
            holder = FileLock(root)
            holder.acquire(0)
            try:
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "eb_staged_rebuild.cli",
                        "gc",
                        "--state-root",
                        root,
                        "--lock-timeout",
                        "nan",
                    ],
                    capture_output=True,
                )
                with self.assertRaises(Refused):
                    FileLock(root).acquire(0)
            finally:
                holder.release()
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, b"")
            self.assertIn(b"--lock-timeout is not a finite number", proc.stderr)
            self.assertNotIn(b"holds the lock", proc.stderr)


if __name__ == "__main__":
    unittest.main()
