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

import os
import stat

from eb_staged_rebuild.errors import Refused
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


def _store(path, data, mode):
    writable = mode & stat.S_IWUSR
    if not writable:
        os.chmod(path, mode | stat.S_IWUSR)
    try:
        with open(path, "r+b") as handle:
            handle.seek(0)
            handle.write(data)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if not writable:
            os.chmod(path, mode)


def _rewrite_one(path, old, new):
    if os.path.basename(path) in _SKIP:
        return
    kind = lstat_kind(path)
    if kind == "symlink":
        target = os.readlink(path)
        updated, count = replace_prefix(target.encode("utf-8", "surrogateescape"), old, new)
        if count == 0:
            return
        os.unlink(path)
        os.symlink(updated.decode("utf-8", "surrogateescape"), path)
        return
    if kind != "file":
        return
    with open(path, "rb") as handle:
        data = handle.read()
    if old not in data:
        return
    if b"\x00" in data:
        updated = _rewrite_binary(data, old, new, path)
    else:
        updated, _count = replace_prefix(data, old, new)
    if updated == data:
        return
    _store(path, updated, stat.S_IMODE(os.lstat(path).st_mode))


def rewrite_tree(root, old, new):
    """Rewrite ``old`` to ``new`` under ``root``. Symbolic links are not followed."""
    if old == new:
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for name in dirnames:
            child = os.path.join(dirpath, name)
            if os.path.islink(child):
                _rewrite_one(child, old, new)
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            _rewrite_one(os.path.join(dirpath, name), old, new)


def rewrite_install_prefix(staged, live):
    """Rewrite one staged install so its files name ``live``'s prefix."""
    old, new = prefix_pair(staged, live)
    rewrite_tree(staged, old.encode("utf-8"), new.encode("utf-8"))
