"""Serialize a state root with a local gate and a POSIX record lock.

The flock gate excludes threads and processes sharing a kernel. The distinct
POSIX sidecar excludes nodes when the filesystem provides coherent record
locks. Open the sidecar only while holding the gate: closing any descriptor
for its inode drops this process's record locks. Other code in this process
must not open that sidecar. Release and close it before releasing the gate.

Never unlink either lock file while the state root is in use. Every node must
use this protocol; flock-only holders on other nodes do not participate.
The filesystem controls recovery after node failure. Fork children must close
inherited descriptors without unlocking the parent's open file description.
"""

import errno
import fcntl
import os
import stat
import time

from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.paths import assert_real_components

LOCK_RETRY_INTERVAL_SECONDS = 0.02


def _take(lock_fn, fd, deadline):
    if deadline is None:
        lock_fn(fd, fcntl.LOCK_EX)
        return
    while True:
        try:
            lock_fn(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            remaining = deadline - time.monotonic()
            if not (remaining > 0):
                raise TimeoutError from exc
            time.sleep(min(LOCK_RETRY_INTERVAL_SECONDS, remaining))


class FileLock:
    """One instance per acquirer; do not share an instance between threads."""

    def __init__(self, state_root):
        self.state_root = os.fspath(state_root)
        self.path = os.path.join(self.state_root, ".lock")
        self.posix_path = self.path + ".posix"
        self.fd = None
        self.posix_fd = None
        self._owner = None

    def _require_private(self, dir_fd, name, label):
        """Refuse a lock name that is not a regular file with one link.

        ``lstat`` does not open the inode, so a FIFO cannot block the
        acquirer. The check is repeated on the descriptor that will be
        truncated: a hard link planted on ``.lock`` would otherwise lose
        the bytes of the other name.
        """
        try:
            info = os.lstat(name, dir_fd=dir_fd)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Refused(f"{label}: {os.path.join(self.state_root, name)}")

    def acquire(self, timeout=None):
        """Take the state root's lock, waiting at most ``timeout`` seconds, or without limit.

        A state root with a symbolic link in its path, or lock files that are not private regular
        files, are refused, and so is a lock another process still holds when the time is up.
        """
        if self.fd is not None:
            raise RuntimeError(f"lock at {self.path} is already held by this object")
        reason = assert_real_components(self.state_root)
        if reason:
            raise Refused(f"state root is unsafe: {reason}")
        os.makedirs(self.state_root, exist_ok=True)
        reason = assert_real_components(self.state_root)
        if reason:
            raise Refused(f"state root is unsafe: {reason}")
        deadline = None if timeout is None else time.monotonic() + timeout
        gate = posix = directory = None
        try:
            directory = os.open(
                self.state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            self._require_private(directory, ".lock", "lock file must be a private regular file")
            self._require_private(
                directory, ".lock.posix", "record lock requires a private regular file"
            )
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            gate = os.open(".lock", flags, 0o600, dir_fd=directory)
            info = os.fstat(gate)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Refused(f"lock file must be a private regular file: {self.path}")
            os.fchmod(gate, 0o600)
            _take(fcntl.flock, gate, deadline)
            posix = os.open(".lock.posix", flags, 0o600, dir_fd=directory)
            info = os.fstat(posix)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Refused(f"record lock requires a private regular file: {self.posix_path}")
            os.fchmod(posix, 0o600)
            _take(fcntl.lockf, posix, deadline)
            info = os.fstat(gate)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Refused(f"lock file must be a private regular file: {self.path}")
            os.ftruncate(gate, 0)
            os.write(gate, f"{os.getpid()}\n".encode())
        except BaseException as exc:
            self._drop(gate, posix, unlock=False)
            if directory is not None:
                os.close(directory)
            if isinstance(exc, TimeoutError):
                raise Refused(
                    f"another eb-staged-rebuild holds the lock at {self.path}"
                ) from exc
            raise
        os.close(directory)
        self.fd, self.posix_fd, self._owner = gate, posix, os.getpid()

    def release(self):
        """Let go of the lock.

        A process that inherited it, such as a forked child, closes its descriptors without
        unlocking.
        """
        gate, posix = self.fd, self.posix_fd
        if gate is None:
            return
        inherited = self._owner != os.getpid()
        self.fd = self.posix_fd = self._owner = None
        self._drop(gate, posix, unlock=not inherited)

    @staticmethod
    def _drop(gate, posix, unlock):
        try:
            if posix is not None:
                try:
                    if unlock:
                        fcntl.lockf(posix, fcntl.LOCK_UN)
                finally:
                    os.close(posix)
        finally:
            if gate is not None:
                try:
                    if unlock:
                        fcntl.flock(gate, fcntl.LOCK_UN)
                finally:
                    os.close(gate)
