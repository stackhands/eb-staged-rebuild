"""Find the generation directory path in a staged tree.

A build that records the generation directory leaves paths that name nothing
once publication renames the tree. Three kinds of record are looked for in
every regular file of a staged installation:

* ``rpath``: an ELF ``DT_RPATH`` entry that names the directory.
* ``runpath``: an ELF ``DT_RUNPATH`` entry that names it.
* ``interp``: an ELF ``PT_INTERP`` that names it.
* ``text``: the directory path in any other file, as a byte string.

An ELF file is read through its program headers only. A match must end at the
end of the data or at a byte that cannot continue a path component, so the
directory ``generations/r1`` is not found in ``generations/r10``.
"""

import os
import stat
import struct

from eb_staged_rebuild.errors import Refused

# Hits sort by path, then by this order.
KINDS = ("rpath", "runpath", "interp", "text")
# Bytes that continue a path component. A match followed by one of them is a
# longer name, not this directory.
_NAME_BYTES = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._+-")
CHUNK = 1 << 20
MAX_STRING = 1 << 16
MAX_DYNAMIC = 1 << 20
MAX_PROGRAM_HEADERS = 1024
MAX_PROGRAM_TABLE = 1 << 16
MAX_OFFSET = 1 << 63
MAX_INTERP = 4096
STAMP = ".eb-staged-rebuild-complete"
# The stamp and the temporary file an interrupted write of it leaves. Both
# hold the generation path and belong to this tool, not to the build.
OWN_FILES = (STAMP, f".{STAMP}.new")

_PT_LOAD, _PT_DYNAMIC, _PT_INTERP = 1, 2, 3
_DT_NULL, _DT_STRTAB, _DT_RPATH, _DT_RUNPATH = 0, 5, 15, 29


def _has_path(data, needle):
    """``needle`` in ``data``, followed by a non-name byte or the end of data."""
    start = 0
    while True:
        index = data.find(needle, start)
        if index < 0:
            return False
        end = index + len(needle)
        if end >= len(data) or data[end] not in _NAME_BYTES:
            return True
        start = index + 1


def _text_has(fd, needle):
    carry = b""
    while True:
        chunk = os.read(fd, CHUNK)
        final = not chunk
        buf = carry + chunk
        start = 0
        while True:
            index = buf.find(needle, start)
            if index < 0:
                break
            end = index + len(needle)
            if end < len(buf):
                if buf[end] not in _NAME_BYTES:
                    return True
            elif final:
                return True
            start = index + 1
        if final:
            return False
        carry = buf[-len(needle):]


def _read_at(fd, offset, size):
    """Exactly ``size`` bytes at ``offset``, or None."""
    if size < 0 or offset < 0 or offset >= MAX_OFFSET:
        return None
    data = os.pread(fd, size, offset)
    return data if len(data) == size else None


def _cstring_at(fd, offset, limit):
    if offset < 0 or offset >= MAX_OFFSET:
        return None
    data = os.pread(fd, limit, offset)
    if not data:
        return None
    return data.split(b"\0", 1)[0]


def _elf_kinds(fd, needle):
    head = os.pread(fd, 64, 0)
    if len(head) < 52:
        return []
    cls, order = head[4], head[5]
    if order == 1:
        prefix = "<"
    elif order == 2:
        prefix = ">"
    else:
        return []
    if cls == 2 and len(head) == 64:
        phoff, = struct.unpack_from(prefix + "Q", head, 32)
        phentsize, phnum = struct.unpack_from(prefix + "HH", head, 54)
        minimum = 56
    elif cls == 1:
        phoff, = struct.unpack_from(prefix + "I", head, 28)
        phentsize, phnum = struct.unpack_from(prefix + "HH", head, 42)
        minimum = 32
    else:
        return []
    if phentsize < minimum or phnum > MAX_PROGRAM_HEADERS or phentsize * phnum > MAX_PROGRAM_TABLE:
        return []
    table = _read_at(fd, phoff, phentsize * phnum)
    if table is None:
        return []
    loads, interp, dynamic = [], None, None
    for index in range(phnum):
        base = index * phentsize
        if cls == 2:
            kind, _flags, offset, vaddr, _paddr, filesz = struct.unpack_from(prefix + "IIQQQQ", table, base)
        else:
            kind, offset, vaddr, _paddr, filesz = struct.unpack_from(prefix + "IIIII", table, base)
        if kind == _PT_LOAD:
            loads.append((vaddr, offset, filesz))
        elif kind == _PT_INTERP and interp is None:
            interp = (offset, filesz)
        elif kind == _PT_DYNAMIC and dynamic is None:
            dynamic = (offset, filesz)
    kinds = []
    if dynamic is not None:
        entries = _read_at(fd, dynamic[0], min(dynamic[1], MAX_DYNAMIC))
        size = 16 if cls == 2 else 8
        layout = prefix + ("qQ" if cls == 2 else "iI")
        strtab = rpath = runpath = None
        for base in range(0, len(entries or b"") - size + 1, size):
            tag, value = struct.unpack_from(layout, entries, base)
            if tag == _DT_NULL:
                break
            if tag == _DT_STRTAB and strtab is None:
                strtab = value
            elif tag == _DT_RPATH and rpath is None:
                rpath = value
            elif tag == _DT_RUNPATH and runpath is None:
                runpath = value
        table_offset = None
        if strtab is not None:
            for vaddr, offset, filesz in loads:
                if vaddr <= strtab < vaddr + filesz:
                    table_offset = offset + (strtab - vaddr)
                    break
        if table_offset is not None:
            for kind, value in (("rpath", rpath), ("runpath", runpath)):
                if value is None:
                    continue
                text = _cstring_at(fd, table_offset + value, MAX_STRING)
                if text is not None and _has_path(text, needle):
                    kinds.append(kind)
    if interp is not None:
        text = _cstring_at(fd, interp[0], min(interp[1], MAX_INTERP))
        if text is not None and _has_path(text, needle):
            kinds.append("interp")
    return kinds


def _file_kinds(path, needle):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return []
        if os.pread(fd, 4, 0) == b"\x7fELF":
            return _elf_kinds(fd, needle)
        os.lseek(fd, 0, os.SEEK_SET)
        return ["text"] if _text_has(fd, needle) else []
    finally:
        os.close(fd)


def scan_tree(root, base, generation_root):
    """Return ``[(path relative to base, kind)]`` for every record in ``root``."""
    needle = os.fsencode(generation_root)
    hits = []
    stack = [(root, True)]
    while stack:
        directory, top = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise Refused(f"cannot scan {os.path.relpath(directory, base)}: not readable") from exc
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append((entry.path, False))
            elif entry.is_file(follow_symlinks=False):
                if top and entry.name in OWN_FILES:
                    continue
                try:
                    kinds = _file_kinds(entry.path, needle)
                except OSError as exc:
                    raise Refused(f"cannot scan {os.path.relpath(entry.path, base)}: not readable") from exc
                rel = os.path.relpath(entry.path, base)
                hits.extend((rel, kind) for kind in kinds)
    return hits


def sort_hits(hits):
    return sorted(hits, key=lambda hit: (os.fsencode(hit[0]), KINDS.index(hit[1])))
