"""A directory flush that fails stops publication before the live swap.

The install directory is flushed once while the build is recorded.
The next flush has to make the rewritten file's directory entry
durable before that name points at the new inode; a failure there
refuses publication.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import unittest

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faultfs.c")
from tests.support import _compile_fuse_faultfs, _fuse_ready, _skip, _wait_mount


class PrefixDirFsyncTests(unittest.TestCase):
    def test_failed_directory_flush_leaves_the_live_tree(self) -> None:
        if os.geteuid() == 0:
            _skip(self, "the filesystem is mounted by an unprivileged user")
        reason = _fuse_ready()
        if reason:
            _skip(self, reason)
        tool = os.environ.get("TOOL_BIN")
        if not tool:
            _skip(self, "TOOL_BIN is unset")
        binary = self._run(tool)
        detail_bin = binary.stdout + binary.stderr
        self.assertEqual(binary.returncode, 2, detail_bin)
        self.assertIn("Input/output error", detail_bin)
        self.assertIn("LIVE old", detail_bin)
        self.assertIn("generations/r1", detail_bin)

    def _run(self, tool: str) -> subprocess.CompletedProcess[str]:
        work = f"/tmp/dir-fsync-{os.getpid()}-zig"
        mnt = os.path.join(work, "mnt")
        if os.path.isdir(mnt) and os.path.ismount(mnt):
            subprocess.run(["fusermount3", "-u", mnt], check=False, timeout=30)
        shutil.rmtree(work, ignore_errors=True)
        back = os.path.join(work, "back")
        os.makedirs(os.path.join(back, "software", "demo", "1"))
        os.makedirs(os.path.join(back, "modules", "all", "demo"))
        with open(os.path.join(back, "software", "demo", "1", "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("old\n")
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
                    printf '%s\\n' "$sw" > "$sw/demo/1/kept.txt"
                    printf 'local root = "%s/demo/1"\\n' "$sw" > "$mods/all/demo/1.lua"
                    """
                )
            )
        os.chmod(eb, 0o755)
        binary = os.path.join(work, "faultfs")
        _compile_fuse_faultfs(_SRC, binary)
        os.makedirs(mnt)
        env = dict(os.environ)
        env["FAULTFS_FSYNC_DIR"] = "/generations/r1/software/demo/1"
        env["FAULTFS_FSYNC_DIR_SKIP"] = "1"
        env.pop("FAULTFS_FALLOCATE", None)
        env.pop("FAULTFS_SEAL", None)
        mounted = subprocess.Popen(
            [binary, back, mnt],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            _wait_mount(self, mnt, mounted)
            with open(os.path.join(mnt, "modules", "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
                handle.write(f'local root = "{mnt}/software/demo/1"\n')
            done = subprocess.run(
                [
                    tool,
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
                timeout=60,
            )
            live = os.path.join(mnt, "software", "demo", "1", "PAYLOAD")
            if os.path.isfile(live):
                with open(live, encoding="utf-8") as handle:
                    live_text = handle.read().rstrip()
            else:
                live_text = "missing"
            kept = os.path.join(mnt, "generations", "r1", "software", "demo", "1", "kept.txt")
            if os.path.isfile(kept):
                with open(kept, encoding="utf-8") as handle:
                    kept_text = handle.read().rstrip()
            else:
                kept_text = "missing"
        finally:
            subprocess.run(["fusermount3", "-u", mnt], check=False, timeout=30)
            if mounted.stderr is not None and not mounted.stderr.closed:
                mounted.stderr.close()
            try:
                mounted.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mounted.kill()
        stdout = done.stdout.decode("utf-8", "surrogateescape") + f"\nLIVE {live_text}\nKEPT {kept_text}\n"
        stderr = done.stderr.decode("utf-8", "surrogateescape")
        return subprocess.CompletedProcess(done.args, done.returncode, stdout, stderr)

if __name__ == "__main__":
    unittest.main()
