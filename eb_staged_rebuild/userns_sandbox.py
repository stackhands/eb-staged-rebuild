"""Enter a user/mount-namespace sandbox, make the live trees read-only and mount generation trees over stable paths."""

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
# include/uapi/linux/sched.h.
CLONE_NEWUSER = 0x10000000
# GNU timeout and container runtimes conventionally reserve 125 for setup errors.
SANDBOX_SETUP_EXIT = 125


def _mount(source, target, flags):
    libc = ctypes.CDLL(None, use_errno=True)
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


def _bind(source, target):
    """Mount the directory ``source`` over the directory ``target``."""
    source = os.path.abspath(source)
    target = os.path.abspath(target)
    for path in (source, target):
        if not os.path.isdir(path) or os.path.islink(path):
            raise RuntimeError(f"bind mount path is not a real directory: {path}")
    _mount(source, target, MS_BIND | MS_REC)


def _write_proc(name, text):
    with open(f"/proc/self/{name}", "w", encoding="utf-8") as handle:
        handle.write(text)


def _drop_to(spec):
    """Enter a nested user namespace in which the invoking user's ids are the only ids.

    The mount namespace stays, so the read-only and bind mounts stay in force.
    EasyBuild refuses to run as root, which the first namespace's mapping makes
    the process.
    """
    try:
        uid_text, gid_text = spec.split(":")
        uid, gid = int(uid_text), int(gid_text)
    except ValueError:
        raise RuntimeError(f"--as-user needs UID:GID, got {spec!r}") from None
    libc = ctypes.CDLL(None, use_errno=True)
    ctypes.set_errno(0)
    if libc.unshare(CLONE_NEWUSER) != 0:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, f"unshare user namespace: {os.strerror(error)}")
    _write_proc("setgroups", "deny")
    _write_proc("uid_map", f"{uid} 0 1\n")
    _write_proc("gid_map", f"{gid} 0 1\n")
    if os.getuid() != uid or os.getgid() != gid:
        raise RuntimeError(f"user namespace gives ids {os.getuid()}:{os.getgid()}, expected {uid}:{gid}")


def _probe(as_user=None):
    # The mount disappears with this private namespace. Trying to remove its
    # mountpoint before namespace exit would correctly fail with EBUSY.
    root = tempfile.mkdtemp()
    _protect(root)
    try:
        with open(os.path.join(root, "must-fail"), "w", encoding="utf-8"):
            pass
    except OSError as error:
        if error.errno == errno.EROFS:
            if as_user:
                _drop_to(as_user)
            return 0
        raise
    raise RuntimeError("read-only bind mount remained writable")


def _parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--read-only", action="append", default=[])
    parser.add_argument("--bind", action="append", nargs=2, default=[], metavar=("SOURCE", "TARGET"))
    parser.add_argument("--as-user", metavar="UID:GID")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        _make_mounts_private()
        if args.probe:
            return _probe(args.as_user)
        for path in args.read_only:
            _protect(path)
        for source, target in args.bind:
            _bind(source, target)
        if args.as_user:
            _drop_to(args.as_user)
        command = list(args.command)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            raise RuntimeError("no sandbox command was supplied")
        os.execvpe(command[0], command, os.environ)
    except (OSError, RuntimeError) as error:
        print(f"eb-staged-rebuild user namespace: {error}", file=sys.stderr)
        return SANDBOX_SETUP_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
