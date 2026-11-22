"""Point recorded install paths at the stable prefix before publication.

``installpath`` and ``userns`` tell EasyBuild to install under
``<state-root>/generations/<run-id>``. The build then writes that directory
into RPATH, pkg-config, shebangs and other files. Publication renames the
directory to ``software/<name>/<version>`` and, until this step, rewrites
only the module file. The bytes would name a path that no longer exists.

The generation prefix is longer than the stable one whenever the run id is
part of it, so a binary's C string can be shortened in place. A text file is
rewritten at whatever length the stable path needs. The completion stamp is
left alone: it is how recovery tells this run's tree from the previous one.
"""

import ctypes
import errno
import os
import stat

from eb_staged_rebuild.atomicio import _restore_metadata, fsync_dir, read_metadata
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.faults import FaultInjector
from eb_staged_rebuild.paths import abspath, lstat_kind

# A path token ends at anything that is not these characters, matching
# modulefile.replace_path so ``demo/1`` is not changed inside ``demo/10``.
_PATH_CHARS = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._+-")
_SKIP = {".eb-staged-rebuild-complete"}


def prefix_pair(staged, live):
    """Return ``(generation_prefix, stable_prefix)`` for one install directory.

    ``.../generations/<run-id>/software/name/version`` and
    ``.../software/name/version`` share the ``name/version`` suffix, so the
    prefixes are the two software roots.
    """
    staged_parts = [part for part in abspath(staged).split("/") if part]
    live_parts = [part for part in abspath(live).split("/") if part]
    shared = 0
    while shared < len(staged_parts) and shared < len(live_parts) and staged_parts[-1 - shared] == live_parts[-1 - shared]:
        shared += 1
    if shared == 0:
        raise Refused(f"staged path {staged} and live path {live} share no directory suffix")
    old = "/" + "/".join(staged_parts[:-shared])
    new = "/" + "/".join(live_parts[:-shared])
    if not old or not new:
        raise Refused("refusing to rewrite an empty install prefix")
    return old, new


def replace_prefix(data, old, new):
    """Replace path-bounded occurrences of ``old`` with ``new``.

    The replacement is not scanned again, so a stable path that contains the
    generation path is written once. Returns ``(bytes, count)``.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    old_b = old.encode("utf-8") if isinstance(old, str) else old
    new_b = new.encode("utf-8") if isinstance(new, str) else new
    if old_b == new_b:
        return data, 0
    if not old_b:
        raise Refused("refusing to rewrite an empty install prefix")
    if b"\n" in new_b or b'"' in new_b or b"\x00" in new_b:
        raise Refused("refusing to write a prefix that contains a quote, newline, or NUL")
    out = bytearray()
    count = 0
    index = 0
    while index < len(data):
        if data.startswith(old_b, index):
            after = index + len(old_b)
            left_ok = index == 0 or data[index - 1] not in _PATH_CHARS
            right_ok = after >= len(data) or data[after] not in _PATH_CHARS
            if left_ok and right_ok:
                out += new_b
                index = after
                count += 1
                continue
        out.append(data[index])
        index += 1
    return bytes(out), count


def _rewrite_binary(data, old, new, path):
    """Shorten C strings in place. The bytes after each original NUL stay put,
    which is what an ELF string table needs.
    """
    buf = bytearray(data)
    begin = 0
    while begin < len(buf):
        end = buf.find(b"\x00", begin)
        if end < 0:
            end = len(buf)
        updated, count = replace_prefix(bytes(buf[begin:end]), old, new)
        if count and len(updated) > (end - begin):
            raise Refused(
                f"{path} records the generation path in a binary, and the stable "
                f"prefix is longer than that string"
            )
        if count and end == len(buf) and len(updated) != (end - begin):
            raise Refused(
                f"{path} records the generation path in a binary that has no room to shorten it"
            )
        if count and len(updated) == (end - begin):
            buf[begin:end] = updated
        elif count and len(updated) < (end - begin):
            buf[begin : begin + len(updated)] = updated
            buf[begin + len(updated)] = 0
            for gap in range(begin + len(updated) + 1, end):
                buf[gap] = 0
        begin = end + 1
    return bytes(buf)


# A publication never rewrites its own temporary names, or the stamp that
# recovery uses to tell this generation from the previous one.
_TEMP_SUFFIXES = (".ebsr-partial", ".ebsr-ready", ".ebsr-symlink", ".ebsr-link")
_MAX_BYTES = 1 << 30
_PUNCH = 4096


def _ours(name):
    return name in _SKIP or any(name.endswith(suffix) for suffix in _TEMP_SUFFIXES)


def _with_writable_dir(directory, write):
    """Run ``write`` while ``directory`` is owner-writable, then restore it.

    The directory is opened ``O_NOFOLLOW`` and changed with ``fchmod``, so a
    symlink is never chmod'd and the file inside it is never chmod'd either.
    chmod on the directory can replace an NFSv4 ACL; the ACL bytes are put
    back after the mode.
    """
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        if mode & stat.S_IWUSR:
            return write()
        acls = _directory_acls(fd)
        try:
            os.fchmod(fd, mode | stat.S_IWUSR | stat.S_IXUSR)
        except OSError as error:
            _raise_os(error, directory)
        try:
            return write()
        finally:
            try:
                os.fchmod(fd, mode)
            except OSError as error:
                _raise_os(error, directory)
            _restore_directory_acls(fd, acls)
    finally:
        os.close(fd)


def _directory_acls(fd):
    found = []
    try:
        names = os.listxattr(fd)
    except OSError as error:
        if error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            return found
        raise
    for name in names:
        if name.startswith("system.") and "acl" in name:
            found.append((name, os.getxattr(fd, name)))
    return found


def _restore_directory_acls(fd, acls):
    for name, value in acls:
        os.setxattr(fd, name, value)


def _user_xattrs(fd):
    found = []
    try:
        names = os.listxattr(fd)
    except OSError as error:
        if error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            return found
        raise
    for name in names:
        if name.startswith(("user.", "trusted.")):
            found.append((name, os.getxattr(fd, name)))
    return found


def _set_user_xattrs(fd, attrs):
    for name, value in attrs:
        os.setxattr(fd, name, value)


def _punch(fd, offset, length):
    if length <= 0:
        return
    libc = ctypes.CDLL(None, use_errno=True)
    libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int64]
    libc.fallocate.restype = ctypes.c_int
    # FALLOC_FL_KEEP_SIZE | FALLOC_FL_PUNCH_HOLE. A filesystem that cannot
    # punch leaves the zeros in place; the bytes are still correct.
    if libc.fallocate(fd, 3, offset, length) != 0:
        error = ctypes.get_errno()
        if error not in (errno.EOPNOTSUPP, errno.ENOTSUP, errno.EINVAL, errno.ENOSYS):
            raise OSError(error, f"fallocate(PUNCH_HOLE): {os.strerror(error)}")


def _punch_zeros(fd, data):
    index = 0
    while index < len(data):
        if data[index] != 0:
            index += 1
            continue
        end = index
        while end < len(data) and data[end] == 0:
            end += 1
        start = (index + _PUNCH - 1) & ~(_PUNCH - 1)
        aligned = end & ~(_PUNCH - 1)
        if aligned > start:
            _punch(fd, start, aligned - start)
        index = end


def _extents(fd, size):
    """Allocated ranges of ``fd``. A hole is not read, so a sparse file is
    not materialised while it is searched.
    """
    ranges = []
    position = 0
    while position < size:
        try:
            start = os.lseek(fd, position, os.SEEK_DATA)
        except OSError as error:
            if error.errno == errno.ENXIO:
                break
            start = position
            end = size
            os.lseek(fd, start, os.SEEK_SET)
            ranges.append((start, os.read(fd, end - start)))
            break
        try:
            end = os.lseek(fd, start, os.SEEK_HOLE)
        except OSError:
            end = size
        os.lseek(fd, start, os.SEEK_SET)
        blob = bytearray()
        remaining = end - start
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            blob += chunk
            remaining -= len(chunk)
        ranges.append((start, bytes(blob)))
        position = end
    return ranges


def _contains(ranges, size, old):
    if not old:
        return False
    tail = b""
    cursor = 0
    keep = len(old) - 1
    for start, blob in ranges:
        gap = start - cursor
        if gap:
            window = tail + (b"\x00" * min(gap, keep))
            if old in window:
                return True
            tail = window[-keep:] if keep else b""
        window = tail + blob
        if old in window:
            return True
        tail = window[-keep:] if keep else b""
        cursor = start + len(blob)
    if size > cursor and old in tail + (b"\x00" * min(size - cursor, keep)):
        return True
    return False


def _image(ranges, size):
    buf = bytearray(size)
    for start, blob in ranges:
        buf[start:start + len(blob)] = blob
    return bytes(buf)


def _transformed(image, old, new, path):
    if b"\x00" in image:
        return _rewrite_binary(image, old, new, path)
    updated, _count = replace_prefix(image, old, new)
    return updated


def _discard(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _raise_os(error, path):
    """Re-raise ``error`` with ``path`` when the call recorded no filename.

    ``os.write``, ``os.fsync`` and ``os.fchmod`` leave ``filename`` empty, so
    a full or read-only filesystem otherwise prints an errno and no path.
    """
    if error.filename is not None:
        raise error
    raise OSError(error.errno, error.strerror, path) from error


def _commit(names, ready):
    """Point every name at ``ready``'s inode, then drop ``ready``'s directory entry."""
    if len(names) == 1:
        parent = os.path.dirname(names[0]) or "."
        os.replace(ready, names[0])
        fsync_dir(parent)
        return
    for name in names:
        parent = os.path.dirname(name) or "."
        temporary = os.path.join(parent, f".{os.path.basename(name)}.{os.getpid()}.ebsr-link")

        def publish_one(name=name, parent=parent, temporary=temporary):
            if os.path.lexists(temporary):
                if lstat_kind(temporary) != "file" or os.lstat(temporary).st_nlink != 1:
                    raise Refused(f"temporary link is not a private file: {temporary}")
                os.unlink(temporary)
            os.link(ready, temporary)
            try:
                os.replace(temporary, name)
                fsync_dir(parent)
            except BaseException:
                _discard(temporary)
                raise

        _with_writable_dir(parent, publish_one)
    # The test artifact names this step in EB_STAGED_REBUILD_FAIL_AT. The
    # release binary has no such step. A directory made unwritable before
    # the rewrite is opened and made writable for the whole publish, so the
    # mode cannot single out this unlink.
    if "prefix.ready.unlink" in FaultInjector.failing:
        raise OSError(errno.EACCES, "Permission denied", ready)
    os.unlink(ready)
    fsync_dir(os.path.dirname(ready) or ".")


def _publish(names, image, source):
    """Replace ``names`` (one inode) with ``image``, keeping access metadata.

    The bytes land in a temporary file first. ``names`` change only when that
    file is complete, so a crash or a failed rename leaves the previous bytes.
    A read-only file is not chmod'd: chmod would replace an NFSv4 ACL.
    """
    primary = names[0]
    parent = os.path.dirname(primary) or "."
    inode = os.lstat(primary).st_ino
    partial = os.path.join(parent, f".{inode}.ebsr-partial")
    ready = os.path.join(parent, f".{inode}.ebsr-ready")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        attrs = _user_xattrs(fd)
    finally:
        os.close(fd)

    def publish():
        for leftover in (partial, ready):
            if os.path.lexists(leftover) and lstat_kind(leftover) == "file":
                os.unlink(leftover)
        metadata = read_metadata(source)
        mode = stat.S_IMODE(metadata.info.st_mode)
        fd = os.open(partial, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            view = memoryview(image)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            # User attributes go on while the temporary file is still
            # writable. Restoring the recorded mode may clear that bit, and
            # a later chmod would replace an NFSv4 ACL.
            _punch_zeros(fd, image)
            _set_user_xattrs(fd, attrs)
            _restore_metadata(fd, metadata, mode)
            os.fsync(fd)
        except OSError as error:
            os.close(fd)
            _discard(partial)
            _raise_os(error, partial)
        except BaseException:
            os.close(fd)
            _discard(partial)
            raise
        os.close(fd)
        try:
            current = read_metadata(source)
        except (OSError, Refused) as error:
            _discard(partial)
            raise Refused(f"cannot recheck access metadata for {source}: {error}") from error
        if (
            (current.info.st_uid, current.info.st_gid, stat.S_IMODE(current.info.st_mode))
            != (metadata.info.st_uid, metadata.info.st_gid, mode)
            or current.acls != metadata.acls
        ):
            _discard(partial)
            raise Refused(f"access metadata of {source} changed before it was replaced")
        try:
            os.replace(partial, ready)
            fsync_dir(parent)
            _commit(names, ready)
        except BaseException:
            _discard(partial)
            if os.path.lexists(ready):
                ready_inode = os.lstat(ready).st_ino
                held = any(
                    os.path.lexists(name) and not os.path.islink(name) and os.lstat(name).st_ino == ready_inode
                    for name in names
                )
                if not held:
                    _discard(ready)
            raise

    _with_writable_dir(parent, publish)


def _rewrite_symlink(path, old, new):
    target = os.fsencode(os.readlink(path))
    updated, count = replace_prefix(target, old, new)
    if count == 0:
        return
    parent = os.path.dirname(path) or "."
    temporary = os.path.join(parent, f".{os.path.basename(path)}.{os.getpid()}.ebsr-symlink")

    def publish():
        if os.path.lexists(temporary):
            if not os.path.islink(temporary):
                raise Refused(f"temporary symlink is not a symlink: {temporary}")
            os.unlink(temporary)
        os.symlink(os.fsdecode(updated), temporary)
        try:
            os.replace(temporary, path)
            fsync_dir(parent)
        except BaseException:
            if os.path.islink(temporary):
                os.unlink(temporary)
            raise

    _with_writable_dir(parent, publish)


def _read_file(path, old, new):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused(f"{path} changed from a regular file during the rewrite")
        if info.st_size > _MAX_BYTES:
            if _contains(_extents(fd, info.st_size), info.st_size, old):
                raise Refused(f"{path} is larger than 1 GiB and records the generation path")
            return None
        ranges = _extents(fd, info.st_size)
    finally:
        os.close(fd)
    if not _contains(ranges, info.st_size, old):
        return None
    image = _image(ranges, info.st_size)
    updated = _transformed(image, old, new, path)
    if updated == image:
        return None
    return updated


def _raise_walk_error(error):
    """Abort when a directory cannot be listed.

    ``os.walk`` continues after that error unless the callback raises.
    The release binary refuses the directory.
    """
    raise error


def _recover(root):
    """Finish a replacement a previous run left in ``.<inode>.ebsr-ready``.

    The name records the inode the replacement had not yet removed. Every
    regular file still on that inode is pointed at the finished temporary
    file. When none remain, the temporary file was already linked into place
    and only its directory entry is left.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=_raise_walk_error):
        dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if name.startswith(".") and name.endswith(".ebsr-ready"):
                found.append(path)
            elif name.endswith((".ebsr-partial", ".ebsr-link", ".ebsr-symlink")):
                _discard(path)
    for ready in found:
        stem = os.path.basename(ready)[1:].split(".", 1)[0]
        try:
            inode = int(stem)
        except ValueError:
            continue
        names = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=_raise_walk_error):
            dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))]
            for name in filenames:
                path = os.path.join(dirpath, name)
                if path == ready or _ours(name):
                    continue
                try:
                    info = os.lstat(path)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_ino == inode:
                    names.append(path)
        if names:
            _commit(sorted(names), ready)
        else:
            _discard(ready)


def rewrite_tree(root, old, new):
    """Rewrite ``old`` to ``new`` under ``root``. Symbolic links are not followed.

    Each regular file is published by renaming a completed temporary file
    over it, or, when several names share the inode, by linking that file
    onto every name. The previous inode stays until the new one is durable.
    A later call finishes a replacement left in ``.<inode>.ebsr-ready`` and
    drops ``.ebsr-link`` and ``.ebsr-symlink`` temporaries, so a killed
    hard-link publish can be repeated.
    """
    if old == new:
        return
    links = []
    files = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=_raise_walk_error):
        kept = []
        for name in dirnames:
            child = os.path.join(dirpath, name)
            if os.path.islink(child):
                links.append(child)
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                links.append(path)
            elif not _ours(name) and lstat_kind(path) == "file":
                files.append(path)
    groups = {}
    order = []
    for path in files:
        info = os.lstat(path)
        key = (info.st_dev, info.st_ino)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(path)
    _recover(root)
    # Recovery may have replaced some names. Regroup what is left.
    files = [path for path in files if lstat_kind(path) == "file" and not os.path.islink(path)]
    groups = {}
    order = []
    for path in files:
        info = os.lstat(path)
        key = (info.st_dev, info.st_ino)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(path)
    for key in order:
        names = sorted(groups[key])
        info = os.lstat(names[0])
        if info.st_nlink != len(names):
            raise Refused(
                f"{names[0]} has {info.st_nlink} hard links and this tree holds "
                f"{len(names)}; refusing to split them"
            )
        updated = _read_file(names[0], old, new)
        if updated is None:
            continue
        _publish(names, updated, names[0])
    for path in links:
        if lstat_kind(path) == "symlink":
            _rewrite_symlink(path, old, new)


def rewrite_install_prefix(staged, live):
    """Rewrite one staged install so its files name ``live``'s prefix."""
    old, new = prefix_pair(staged, live)
    rewrite_tree(staged, old.encode("utf-8"), new.encode("utf-8"))
