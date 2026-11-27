"""A failed punch of a zero page stops the prefix rewrite.

The file is on a FUSE passthrough whose fallocate returns EIO. The mount is the whole state root, so the refusal is the punch
and not a cross-device rename.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest

from tests.support import _compile_fuse_faultfs, _fuse_ready, _skip, _wait_mount

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faultfs.c")

class PrefixPunchEioTests(unittest.TestCase):
    def test_failed_punch_keeps_the_original_file(self) -> None:
        if os.geteuid() == 0:
            _skip(self, "the filesystem is mounted by an unprivileged user")
        reason = _fuse_ready()
        if reason:
            _skip(self, reason)
        tool = os.environ.get("TOOL_BIN")
        if not tool:
            _skip(self, "TOOL_BIN is unset")
        binary = self._run(tool)
        self.assertEqual(binary.returncode, 2, binary.stderr)
        self.assertIn("fallocate(PUNCH_HOLE)", binary.stderr)
        self.assertIn("Input/output error", binary.stderr)
        self.assertIn("generations/r1", binary.stdout)
        self.assertIn("LIVE old", binary.stdout)

    def _run(self, tool: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="punch-eio-zig-") as work:
            back = os.path.join(work, "back")
            mnt = os.path.join(work, "mnt")
            os.makedirs(os.path.join(back, "software", "demo", "1"))
            os.makedirs(os.path.join(back, "modules", "all", "demo"))
            os.makedirs(os.path.join(back, "generations", "r1", "software", "demo", "1"))
            with open(os.path.join(back, "software", "demo", "1", "PAYLOAD"), "w", encoding="utf-8") as handle:
                handle.write("old\n")
            with open(os.path.join(back, "modules", "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
                handle.write(f'local root = "{mnt}/software/demo/1"\n')
            generation = f"{mnt}/generations/r1"
            text = f"prefix={generation}/software/demo/1\n".encode()
            pad = 4096 - (len(text) % 4096)
            blob = text + b"\0" * pad + b"\0" * 8192
            kept_back = os.path.join(back, "generations", "r1", "software", "demo", "1", "kept.txt")
            with open(kept_back, "wb") as handle:
                handle.write(blob)
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
            env["FAULTFS_FALLOCATE"] = "eio"
            env.pop("FAULTFS_SEAL", None)
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
                kept = os.path.join(mnt, "generations", "r1", "software", "demo", "1", "kept.txt")
                if not os.path.isfile(kept):
                    kept = os.path.join(mnt, "software", "demo", "1", "kept.txt")
                if os.path.isfile(kept):
                    with open(kept, "rb") as handle:
                        body = handle.read()[:80]
                else:
                    body = b""
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
            detail = done.stderr.decode("utf-8", "surrogateescape")
            stdout = done.stdout.decode("utf-8", "surrogateescape")
            stdout += f"\nBODY {body!r}\nLIVE {live_text}\n"
            return subprocess.CompletedProcess(done.args, done.returncode, stdout, detail)

if __name__ == "__main__":
    unittest.main()
