"""A sealed install directory that rejects chmod stops publication.

The directory is reported without a write bit, and chmod returns
EPERM. Creating a file there still succeeds, which is what let the
binary write the completion stamp after ignoring the chmod.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faultfs.c")
from tests.support import _compile_fuse_faultfs, _fuse_ready, _skip, _wait_mount


class SealedDirChmodTests(unittest.TestCase):
    def test_refused_chmod_leaves_the_live_tree(self) -> None:
        if os.geteuid() == 0:
            _skip(self, "the filesystem is mounted by an unprivileged user")
        reason = _fuse_ready()
        if reason:
            _skip(self, reason)
        tool = os.environ.get("TOOL_BIN")
        if not tool:
            _skip(self, "TOOL_BIN is unset")
        python = self._run("python")
        binary = self._run(tool)
        detail_py = python.stdout + python.stderr
        detail_bin = binary.stdout + binary.stderr
        self.assertEqual(python.returncode, 2, detail_py)
        self.assertEqual(binary.returncode, 2, detail_bin)
        self.assertIn("Operation not permitted", detail_py)
        self.assertIn("Operation not permitted", detail_bin)
        self.assertIn("LIVE old", detail_py)
        self.assertIn("LIVE old", detail_bin)

    def _run(self, tool: str) -> subprocess.CompletedProcess[str]:
        label = "python" if tool == "python" else "zig"
        with tempfile.TemporaryDirectory(prefix=f"sealed-chmod-{label}-") as work:
            mnt = os.path.join(work, "mnt")
            back = os.path.join(work, "back")
            os.makedirs(os.path.join(back, "software", "demo", "1"))
            os.makedirs(os.path.join(back, "modules", "all", "demo"))
            with open(os.path.join(back, "software", "demo", "1", "PAYLOAD"), "w", encoding="utf-8") as handle:
                handle.write("old\n")
            with open(os.path.join(back, "modules", "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
                handle.write(f'local root = "{mnt}/software/demo/1"\n')
            eb = os.path.join(work, "fake-eb")
            with open(eb, "w", encoding="utf-8") as handle:
                handle.write(
                    textwrap.dedent(
                        """\
                        #!/bin/sh
                        for arg in "$@"; do
                          case "$arg" in
                            --installpath-software=*) sw="${arg#*=}" ;;
                            --installpath-modules=*) mods="${arg#*=}" ;;
                          esac
                        done
                        mkdir -p "$sw/demo/1" "$mods/all/demo"
                        printf 'new\\n' > "$sw/demo/1/PAYLOAD"
                        printf 'local root = "%s/demo/1"\\n' "$sw" > "$mods/all/demo/1.lua"
                        """
                    )
                )
            os.chmod(eb, 0o755)
            binary = os.path.join(work, "faultfs")
            _compile_fuse_faultfs(_SRC, binary)
            os.makedirs(mnt)
            env = dict(os.environ)
            env["FAULTFS_SEAL"] = "/generations/r1/software/demo/1"
            env.pop("FAULTFS_FALLOCATE", None)
            env.pop("FAULTFS_FSYNC_DIR", None)
            env.pop("FAULTFS_FSYNC_DIR_SKIP", None)
            mounted = subprocess.Popen(
                [binary, back, mnt],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
            )
            try:
                _wait_mount(self, mnt, mounted)
                run_env = dict(os.environ)
                run_env["PYTHONPATH"] = _REPO
                run_env["PYTHONIOENCODING"] = "utf-8:strict"
                argv = [sys.executable, "-m", "eb_staged_rebuild"] if tool == "python" else [tool]
                done = subprocess.run(
                    [
                        *argv,
                        "run",
                        "--software",
                        os.path.join(mnt, "software"),
                        "--modules",
                        os.path.join(mnt, "modules"),
                        "--state-root",
                        mnt,
                        "--backend",
                        "installpath",
                        "--eb",
                        eb,
                        "--module",
                        "demo/1",
                        "--run-id",
                        "r1",
                        "--",
                        "demo-1.eb",
                    ],
                    check=False,
                    capture_output=True,
                    env=run_env,
                    timeout=60,
                )
                live = os.path.join(mnt, "software", "demo", "1", "PAYLOAD")
                if os.path.isfile(live):
                    with open(live, encoding="utf-8") as handle:
                        live_text = handle.read().rstrip()
                else:
                    live_text = "missing"
            finally:
                subprocess.run(["fusermount3", "-u", mnt], check=False, timeout=30)
                if mounted.stderr is not None and not mounted.stderr.closed:
                    mounted.stderr.close()
                try:
                    mounted.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    mounted.kill()
            stdout = done.stdout.decode("utf-8", "surrogateescape") + f"\nLIVE {live_text}\n"
            stderr = done.stderr.decode("utf-8", "surrogateescape")
            return subprocess.CompletedProcess(done.args, done.returncode, stdout, stderr)

if __name__ == "__main__":
    unittest.main()
