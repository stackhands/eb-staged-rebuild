"""A before/after content record of a live install directory."""

import hashlib
import os

from eb_staged_rebuild.paths import lstat_kind

HASH_BLOCK_BYTES = 1024 * 1024


def valid_snapshot(value):
    """Return whether ``value`` has the shape produced by :func:`snapshot`."""
    if not isinstance(value, dict) or not isinstance(value.get("exists"), bool):
        return False
    if not value["exists"]:
        return set(value) == {"exists"}
    if not isinstance(value.get("symlink"), bool):
        return False
    if value["symlink"]:
        return set(value) == {"exists", "symlink", "target"} and isinstance(
            value.get("target"), str
        )
    kind = value.get("kind")
    if not isinstance(kind, str):
        return False
    if kind != "dir":
        return set(value) == {"exists", "symlink", "kind"}
    if set(value) != {"exists", "symlink", "kind", "inode", "device", "entries"}:
        return False
    if type(value.get("inode")) is not int or type(value.get("device")) is not int:
        return False
    entries = value.get("entries")
    if not isinstance(entries, list):
        return False
    return all(
        isinstance(row, list)
        and len(row) == 5
        and isinstance(row[0], str)
        and row[1] in ("file", "symlink")
        and type(row[2]) is int
        and type(row[3]) is int
        and isinstance(row[4], str)
        for row in entries
    )


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(HASH_BLOCK_BYTES)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def snapshot(path):
    """Return a record of the tree at ``path``.

    The record has each entry's relative path, kind, inode and size, and a symbolic link's
    target or a file's SHA-256. A missing path, a link or another kind is recorded as such.
    """
    path = os.fspath(path)
    kind = lstat_kind(path)
    if kind == "missing":
        return {"exists": False}
    if kind == "symlink":
        return {"exists": True, "symlink": True, "target": os.readlink(path)}
    if kind != "dir":
        return {"exists": True, "symlink": False, "kind": kind}
    st = os.lstat(path)
    rows = []

    def _walk_error(exc):
        raise OSError(exc.errno, exc.strerror, path) from exc

    for dirpath, dirnames, filenames in os.walk(path, followlinks=False, onerror=_walk_error):
        kept = []
        for name in dirnames:
            child = os.path.join(dirpath, name)
            if os.path.islink(child):
                lst = os.lstat(child)
                rows.append(
                    [
                        os.path.relpath(child, path),
                        "symlink",
                        lst.st_ino,
                        lst.st_size,
                        os.readlink(child),
                    ]
                )
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            child = os.path.join(dirpath, name)
            lst = os.lstat(child)
            kind_name = "symlink" if os.path.islink(child) else "file"
            detail = os.readlink(child) if kind_name == "symlink" else _sha256(child)
            rows.append(
                [
                    os.path.relpath(child, path),
                    kind_name,
                    lst.st_ino,
                    lst.st_size,
                    detail,
                ]
            )
    rows.sort()
    return {
        "exists": True,
        "symlink": False,
        "kind": "dir",
        "inode": st.st_ino,
        "device": st.st_dev,
        "entries": rows,
    }


def same_snapshot(before, after):
    """Return whether two snapshots are equal."""
    return before == after


def empty_real_dir(path):
    """Return whether ``path`` is a real directory with no entries."""
    if lstat_kind(path) != "dir":
        return False
    return not os.listdir(path)
