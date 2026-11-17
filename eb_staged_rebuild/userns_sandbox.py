"""Enter a user/mount-namespace sandbox, make the live trees read-only, and
run the command as the caller."""

import argparse
import ctypes
import errno
import os
import sys
import tempfile


# Linux UAPI include/uapi/linux/mount.h.
MS_RDONLY = 1
MS_REMOUNT = 1 << 5
MS_BIND = 1 << 12
MS_REC = 1 << 14
MS_PRIVATE = 1 << 18
# Linux UAPI include/uapi/linux/sched.h.
CLONE_NEWUSER = 0x10000000
# GNU timeout and container runtimes conventionally reserve 125 for setup errors.
SANDBOX_SETUP_EXIT = 125


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def _mount(source, target, flags):
    libc = _libc()
    mount = libc.mount
    mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    mount.restype = ctypes.c_int
    ctypes.set_errno(0)
    source_bytes = os.fsencode(source) if source is not None else None
    if mount(source_bytes, os.fsencode(target), None, flags, None) != 0:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, f"mount {target}: {os.strerror(error)}")


def _make_mounts_private():
    _mount(None, "/", MS_REC | MS_PRIVATE)


def _protect(path):
    path = os.path.abspath(path)
    if not os.path.isdir(path) or os.path.islink(path):
        raise RuntimeError(f"read-only sandbox path is not a real directory: {path}")
    _mount(path, path, MS_BIND | MS_REC)
    _mount(path, path, MS_BIND | MS_REMOUNT | MS_RDONLY)


def _parent_id(map_path, inner):
    """The id of the parent namespace that ``inner`` maps to in a uid_map or gid_map."""
    with open(map_path, encoding="ascii") as handle:
        for line in handle:
            try:
                first, parent, count = (int(field) for field in line.split())
            except ValueError:
                break
            if first <= inner < first + count:
                return parent + inner - first
    raise RuntimeError(f"{map_path} does not map {inner}")


def _write_proc(path, text):
    with open(path, "w", encoding="ascii") as handle:
        handle.write(text)


def _drop_to_caller():
    """Become the caller's uid and gid again, in a nested user namespace.

    ``unshare --map-root-user`` makes the caller uid 0, which the mounts need
    and which EasyBuild refuses to run as. The nested namespace maps the
    caller's ids onto this namespace's 0, so the command runs as the caller
    and, after ``exec``, without capabilities. The read-only mounts belong to
    this namespace, which the command cannot change.
    """
    uid, gid = os.getuid(), os.getgid()
    caller_uid = _parent_id("/proc/self/uid_map", uid)
    caller_gid = _parent_id("/proc/self/gid_map", gid)
    libc = _libc()
    libc.unshare.argtypes = [ctypes.c_int]
    libc.unshare.restype = ctypes.c_int
    if libc.unshare(CLONE_NEWUSER) != 0:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, f"unshare a user namespace: {os.strerror(error)}")
    _write_proc("/proc/self/setgroups", "deny")
    _write_proc("/proc/self/uid_map", f"{caller_uid} {uid} 1\n")
    _write_proc("/proc/self/gid_map", f"{caller_gid} {gid} 1\n")


def _probe():
    # The mount disappears with this private namespace. Trying to remove its
    # mountpoint before namespace exit would correctly fail with EBUSY.
    root = tempfile.mkdtemp()
    _protect(root)
    try:
        with open(os.path.join(root, "must-fail"), "w", encoding="utf-8"):
            pass
    except OSError as error:
        if error.errno != errno.EROFS:
            raise
    else:
        raise RuntimeError("read-only bind mount remained writable")
    _drop_to_caller()
    return 0


def _parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--read-only", action="append", default=[])
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv=None):
    """Run the user-namespace sandbox and return its exit status.

    The mounts are made private, each ``--read-only`` path is mounted read-only, and the command
    runs as the caller. With ``--probe``, the sandbox only checks that a read-only mount refuses a
    write and that it can become the caller again.
    """
    args = _parser().parse_args(argv)
    try:
        _make_mounts_private()
        if args.probe:
            return _probe()
        for path in args.read_only:
            _protect(path)
        command = list(args.command)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            raise RuntimeError("no sandbox command was supplied")
        _drop_to_caller()
        os.execvpe(command[0], command, os.environ)
    except (OSError, RuntimeError) as error:
        print(f"eb-staged-rebuild user namespace: {error}", file=sys.stderr)
        return SANDBOX_SETUP_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
