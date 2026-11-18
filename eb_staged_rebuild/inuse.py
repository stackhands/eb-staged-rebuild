"""What on this node still uses a tree that gc is about to remove.

Everything comes from ``/proc`` by reading links and text, so the scan never
touches the filesystem of a file it finds. The mount table of gc's own mount namespace
shows a mount point at or inside the tree and a bind mount of the tree or of
a directory in it. Every other mount namespace whose table differs is read
for bind mounts too, so a container that bound the stable path before a
publication swapped it away is seen. Each process's executable, working
directory, root directory, open files and mapped files are compared by
path, and that path is the one the process itself sees: a process in
another mount namespace that reaches the tree under another path is not
seen. A process gc may not inspect, which without root is any other user's,
is skipped. Other nodes are not seen at all.
"""

import os
import re

_ESCAPE = re.compile(rb"\\([0-7]{3})")

_LINKS = (
    (b"exe", "runs a program in it"),
    (b"cwd", "has its working directory in it"),
    (b"root", "has its root directory in it"),
)


def _unescape(field):
    return _ESCAPE.sub(lambda found: bytes([int(found.group(1), 8) & 0xFF]), field)


def mounts(table):
    """The device, root and mount point of each line of a mountinfo file."""
    found = []
    for line in table.split(b"\n"):
        fields = line.split(b" ")
        if len(fields) >= 5:
            found.append((fields[2], _unescape(fields[3]), _unescape(fields[4])))
    return found


def _within(path, top):
    return path == top or path.startswith(top.rstrip(b"/") + b"/")


def source(tree, table):
    """The device of the mount that holds ``tree``, and the tree's path on that device."""
    held = None
    for mount in table:
        if _within(tree, mount[2]) and (held is None or len(mount[2]) >= len(held[2])):
            held = mount
    if held is None:
        return None
    device, root, point = held
    return device, (root.rstrip(b"/") + tree[len(point.rstrip(b"/")) :]) or b"/"


def _read(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _readlink(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def _numbers(directory):
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(int(name) for name in names if name.isdigit())


def mapped(maps):
    """The pathname of each line of a maps file that names one."""
    for line in (maps or b"").split(b"\n"):
        fields = line.split(None, 5)
        if len(fields) == 6:
            yield fields[5]


def users(trees):
    """For each tree, the first use of it this node shows, or None.

    The mounts of gc's own namespace come first, then each process by pid:
    the mounts of its namespace when no earlier process shared them, its
    executable, working directory and root directory, its open files by
    descriptor number, and its mapped files in address order.
    """
    found = dict.fromkeys(trees)
    if not trees:
        return found
    real = {tree: os.fsencode(os.path.realpath(tree)) for tree in trees}
    own = _read("/proc/self/mountinfo") or b""
    table = mounts(own)
    held = {tree: source(real[tree], table) for tree in trees}

    def note(tree, why):
        if found[tree] is None:
            found[tree] = why

    def bound(tree, device, root):
        return held[tree] is not None and device == held[tree][0] and _within(root, held[tree][1])

    for device, root, point in table:
        for tree in trees:
            if _within(point, real[tree]):
                note(tree, f"{os.fsdecode(point)} is a mount point")
            elif bound(tree, device, root):
                note(tree, f"mounted at {os.fsdecode(point)}")
    seen = {own}
    for pid in _numbers(b"/proc"):
        if all(found.values()):
            break
        base = b"/proc/%d/" % pid
        other = _read(base + b"mountinfo")
        if other is not None and other not in seen:
            seen.add(other)
            for device, root, point in mounts(other):
                for tree in trees:
                    if bound(tree, device, root):
                        note(tree, f"mounted at {os.fsdecode(point)} in the mount namespace of pid {pid}")
        uses = [(_readlink(base + name), what) for name, what in _LINKS]
        uses += [(_readlink(base + b"fd/%d" % fd), "has a file open in it") for fd in _numbers(base + b"fd")]
        uses += [(path, "maps a file in it") for path in mapped(_read(base + b"maps"))]
        for path, what in uses:
            for tree in trees:
                if path is not None and _within(path, real[tree]):
                    note(tree, f"pid {pid} {what}")
    return found
