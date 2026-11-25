#!/usr/bin/env python3
"""Stand-in for ``$LMOD_CMD bash load NAME``, as Lmod answers it.

It finds NAME.lua on MODULEPATH and prints each ``setenv`` of the module file
as Lmod's bash output assigns it, then ``_LMFILES_`` and ``LOADEDMODULES``.
An unknown module gets Lmod's error and exit status 1. FAKE_LMOD_EBROOT
replaces the value of every EBROOT* variable.
"""

import os
import re
import sys

_LOCAL = re.compile(r'local\s+(\w+)\s*=\s*"([^"]*)"')
_SETENV = re.compile(r'setenv\(\s*"(\w+)"\s*,\s*(?:"([^"]*)"|(\w+))\s*\)')


def main(argv):
    if len(argv) != 3 or argv[1] != "load":
        print("usage: fake_lmod.py bash load NAME", file=sys.stderr)
        return 2
    name = argv[2]
    for directory in os.environ.get("MODULEPATH", "").split(":"):
        path = os.path.join(directory, name + ".lua")
        if directory and os.path.isfile(path):
            break
    else:
        print("false")
        print(
            "Lmod has detected the following error: The following module(s) are unknown:\n"
            f'"{name}"\n\nPlease check the spelling or version number.',
            file=sys.stderr,
        )
        return 1
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    names = dict(_LOCAL.findall(text))
    for key, literal, variable in _SETENV.findall(text):
        value = literal if not variable else names.get(variable, "")
        if key.startswith("EBROOT") and os.environ.get("FAKE_LMOD_EBROOT"):
            value = os.environ["FAKE_LMOD_EBROOT"]
        print(f"{key}={value};")
        print(f"export {key};")
    print(f"_LMFILES_={path};")
    print("export _LMFILES_;")
    print(f"LOADEDMODULES={name};")
    print("export LOADEDMODULES;")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
