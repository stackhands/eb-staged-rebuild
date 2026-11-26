"""Build the wheel: the Zig binary and the _launch extension.

``zig build -Drelease=true`` runs during the build and its binary is copied to
``eb_staged_rebuild/bin/eb-staged-rebuild``. EB_STAGED_REBUILD_BINARY names a
binary already built, which is copied instead.

The ``_launch`` extension calls execv(2). A GIL CPython builds it against the
limited API (Py_LIMITED_API 3.11), so one wheel tagged cp311-abi3 serves CPython
3.11 and later. A free-threaded CPython installs no abi3 wheel and has no
limited API before 3.15, so there the extension is a plain build tagged cp313t
or cp314t.
"""

import os
import shutil
import subprocess
import sysconfig
from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent
LIMITED = not sysconfig.get_config_var("Py_GIL_DISABLED")


class BuildPy(build_py):
    def run(self):
        super().run()
        prebuilt = os.environ.get("EB_STAGED_REBUILD_BINARY")
        if prebuilt:
            source = Path(prebuilt)
        else:
            subprocess.run(["zig", "build", "-Drelease=true"], cwd=ROOT, check=True)
            source = ROOT / "zig-out" / "bin" / "eb-staged-rebuild"
        target = Path(self.build_lib) / "eb_staged_rebuild" / "bin"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target / "eb-staged-rebuild")
        os.chmod(target / "eb-staged-rebuild", 0o755)


extension = Extension(
    "eb_staged_rebuild._launch",
    sources=["eb_staged_rebuild/_launch.c"],
    define_macros=[("Py_LIMITED_API", "0x030B0000")] if LIMITED else [],
    py_limited_api=LIMITED,
)

options = {"bdist_wheel": {"py_limited_api": "cp311"}} if LIMITED else {}

setup(cmdclass={"build_py": BuildPy}, ext_modules=[extension], options=options)
