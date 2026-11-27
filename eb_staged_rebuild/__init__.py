"""The eb-staged-rebuild command for Python: the Zig binary, found and run.

The wheel carries the binary under ``eb_staged_rebuild/bin``. ``python3 -m
eb_staged_rebuild`` and the ``eb-staged-rebuild`` console script replace the
Python process with it through ``_launch``, a C extension over ``execv(2)``, so there
is one implementation of every command.

``site_hooks`` (an EasyBuild hook file) and ``ship`` (a checksum list for a
copied install) are the Python-only parts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import _launch

__version__ = "3.0.1"

BINARY_NAME = "eb-staged-rebuild"


def binary_path() -> str:
    """The eb-staged-rebuild binary: the one this package carries, else the first on PATH.

    Raises FileNotFoundError, naming both places, when there is neither.
    """
    bundled = Path(__file__).resolve().parent / "bin" / BINARY_NAME
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    found = shutil.which(BINARY_NAME)
    if found:
        return found
    raise FileNotFoundError(
        f"no {BINARY_NAME} binary: {bundled} is missing and {BINARY_NAME} is not on PATH; "
        "install a wheel that carries it, or a release binary"
    )


def run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    """Run the binary with ARGS and return the finished process; keyword arguments go to subprocess.run."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run([binary_path(), *args], check=False, **kwargs)


def run_json(*args: str):
    """Run a command that takes ``--json`` and return its parsed document.

    ``probe``, ``status`` and ``gc`` have the option. Exit status 1 of ``probe`` and
    ``status`` accompanies a document; any other nonzero status raises RuntimeError
    with the command's standard error.
    """
    done = run(*args, "--json")
    if done.returncode not in (0, 1) or not done.stdout.strip():
        raise RuntimeError(f"{BINARY_NAME} {' '.join(args)} exited {done.returncode}: {done.stderr.strip()}")
    return json.loads(done.stdout)


def main(argv: list[str] | None = None) -> int:
    """Replace this process with the binary, given ARGV or this process's arguments."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        binary = binary_path()
    except FileNotFoundError as error:
        sys.stderr.write(f"{BINARY_NAME}: {error}\n")
        return 2
    sys.stdout.flush()
    sys.stderr.flush()
    _launch.execv(binary, [BINARY_NAME, *args])
    return 0  # _launch.execv does not return
