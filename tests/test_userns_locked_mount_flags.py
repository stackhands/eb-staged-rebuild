"""The user-namespace sandbox works under a nosuid or nodev mount.

A user namespace locks a mount's nosuid, nodev, noexec and atime flags. The
read-only remount of the sandbox carries them over; without them it fails with
EPERM, and the sandbox is unusable on any such file system, such as /dev/shm
or a scratch file system mounted nosuid,nodev.
"""

import json
import os
import tempfile
import unittest

from tests.support import _skip, run_cli

LOCKED = os.ST_NOSUID | os.ST_NODEV


def locked_dir():
    """A writable directory on a mount with nosuid or nodev, or None."""
    for base in ("/dev/shm", "/run/user/%d" % os.getuid()):
        try:
            if os.statvfs(base).f_flag & LOCKED and os.access(base, os.W_OK):
                return base
        except OSError:
            continue
    return None


def sandbox_with_tmpdir(tmpdir):
    with tempfile.TemporaryDirectory(dir=tmpdir) as scratch:
        done = run_cli("probe", "--json", "--path", scratch, "--eb", "missing-eb", env={**os.environ, "TMPDIR": scratch})
        if done.returncode not in (0, 1):
            raise AssertionError(done.stderr)
        return json.loads(done.stdout)["userns_sandbox"]


class LockedMountFlagsTests(unittest.TestCase):
    def test_the_sandbox_is_as_usable_under_a_nosuid_mount_as_elsewhere(self):
        base = locked_dir()
        if base is None:
            _skip(self, "no writable nosuid or nodev mount on this host")
        plain = sandbox_with_tmpdir(None)
        if not plain["usable"]:
            _skip(self, f"the user-namespace sandbox is not usable on this host: {plain['reason']}")
        locked = sandbox_with_tmpdir(base)
        self.assertEqual(locked, plain, f"TMPDIR under {base}")


if __name__ == "__main__":
    unittest.main()
