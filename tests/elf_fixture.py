"""Build a tiny ELF file that carries a run path, a runpath and an interpreter.

The file has a PT_LOAD that maps the whole file at address 0, an optional
PT_INTERP and a PT_DYNAMIC with DT_STRTAB, DT_RPATH, DT_RUNPATH and DT_NULL.
It is not loadable code; it holds what a linker records. 32 and 64 bit files,
little and big endian, come from the same layout.
"""

import struct

PT_LOAD, PT_DYNAMIC, PT_INTERP = 1, 2, 3
DT_NULL, DT_STRTAB, DT_RPATH, DT_RUNPATH = 0, 5, 15, 29


def build_elf(rpath=None, runpath=None, interp=None, bits=64, big=False):
    """Return the bytes of an ELF file. A name left as None is not recorded."""
    prefix = ">" if big else "<"
    word = 8 if bits == 64 else 4
    ehsize = 64 if bits == 64 else 52
    phentsize = 56 if bits == 64 else 32
    phnum = 3 if interp is not None else 2
    body_start = ehsize + phentsize * phnum

    interp_bytes = b"" if interp is None else interp.encode() + b"\0"
    strings = b"\0"
    offsets = {}
    for name, value in (("rpath", rpath), ("runpath", runpath)):
        if value is not None:
            offsets[name] = len(strings)
            strings += value.encode() + b"\0"
    interp_offset = body_start
    strtab_offset = interp_offset + len(interp_bytes)
    dynamic_offset = strtab_offset + len(strings)

    entry = prefix + ("qQ" if bits == 64 else "iI")
    dynamic = struct.pack(entry, DT_STRTAB, strtab_offset)
    if "rpath" in offsets:
        dynamic += struct.pack(entry, DT_RPATH, offsets["rpath"])
    if "runpath" in offsets:
        dynamic += struct.pack(entry, DT_RUNPATH, offsets["runpath"])
    dynamic += struct.pack(entry, DT_NULL, 0)
    total = dynamic_offset + len(dynamic)

    ident = b"\x7fELF" + bytes([2 if bits == 64 else 1, 2 if big else 1, 1]) + bytes(9)
    if bits == 64:
        header = ident + struct.pack(prefix + "HHIQQQIHHHHHH", 3, 62, 1, 0, ehsize, 0, 0, ehsize, phentsize, phnum, 0, 0, 0)
    else:
        header = ident + struct.pack(prefix + "HHIIIIIHHHHHH", 3, 3, 1, 0, ehsize, 0, 0, ehsize, phentsize, phnum, 0, 0, 0)

    def program_header(kind, offset, size):
        if bits == 64:
            return struct.pack(prefix + "IIQQQQQQ", kind, 4, offset, offset, offset, size, size, 8)
        return struct.pack(prefix + "IIIIIIII", kind, offset, offset, offset, size, size, 4, 8)

    headers = program_header(PT_LOAD, 0, total)
    if interp is not None:
        headers += program_header(PT_INTERP, interp_offset, len(interp_bytes))
    headers += program_header(PT_DYNAMIC, dynamic_offset, len(dynamic))
    assert word * 2 == len(struct.pack(entry, 0, 0))
    return header + headers + interp_bytes + strings + dynamic
