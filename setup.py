"""Build the wheel with the Zig binary inside it.

``zig build -Drelease=true`` runs during the build and its binary is copied to
``eb_staged_rebuild/bin/eb-staged-rebuild``. EB_STAGED_REBUILD_BINARY names a
binary already built, which is copied instead. The wheel is tagged for the
platform: the binary is static, so it does not depend on the host's libc.
"""

import os
import platform
import shutil
import subprocess
from pathlib import Path

from setuptools import setup
from setuptools.command.bdist_wheel import bdist_wheel
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent


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


class BdistWheel(bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        self.root_is_pure = False

    def get_tag(self):
        return "py3", "none", f"manylinux2014_{platform.machine()}"


setup(cmdclass={"build_py": BuildPy, "bdist_wheel": BdistWheel})
